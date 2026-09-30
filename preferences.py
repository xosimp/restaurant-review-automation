"""preferences — who a setting belongs to: the login, the location, or the
whole organisation (memory audit 9/29/26, owner_layers).

Voice, never-say, the alert matrix, quiet hours, the daily cap and the
briefing settings are columns on each location's restaurants row, and every
save went to the location open at the time. A three-location owner who added
"cheap" to never-say at one location kept seeing it drafted at the other two;
the closing GM who wants 1-star pushes at 11pm and the owner who wants
silence from 10pm had one setting between them; and a group owner could not
turn off a sibling location's brief.

Resolution, for every preference: the LOGIN's own override → the
LOCATION's setting → the ORGANISATION's default → the product default.

  * LOCATION settings stay the restaurants columns every reader already
    reads (models.update_restaurant), so no reader changes.
  * ORGANISATION defaults live in the `preferences` table (scope 'org'),
    written by apply_to_all_locations — which also writes the value onto
    every location in the group, so each one's own column holds it (and a
    location joining the group later takes it: inherit_org_defaults). A
    location whose column differs from the organisation's default is an
    override, and resolve() says so ("this location" vs "all locations").
  * LOGIN overrides (scope 'login', per login and location) are this
    person's own notification choices — push on/off, the alert types they
    mute, their own quiet hours — BOUNDED by the location's: they can only
    take alerts away from their own phone, never turn on a type the owner
    turned off or shorten the owner's quiet hours. push.fire_push applies
    them (push_allowed), so every push path honours them.

One row per (scope, scope_id, key); bounded by the keys, kept until changed.
"""
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# The location settings a group owner may set for every location at once:
# the brand voice, the briefing, quiet hours, the daily cap and the alert
# matrix. Not auto-approve (public posting stays a per-location decision)
# and not the extra alert emails (addresses differ by location).
ORG_KEYS = (
    "voice_notes", "never_say", "sign_off_name",
    "briefing_level", "morning_brief_enabled", "morning_brief_hour", "digest_day", "digest_enabled",
    "alert_quiet_start", "alert_quiet_end", "alert_max_per_day", "alert_hold_during_service",
    "alert_health_bypass_quiet",
    "alert_1star", "alert_2star", "alert_3star", "alert_5star", "alert_health", "alert_neg_spike",
    "alert_negative_trend", "alert_no_response", "alert_any_review", "alert_resp_approved",
    "alert_labor_over", "alert_food_waste", "alert_ai_visibility_drop", "alert_competitor_move",
    "alert_rating_threshold", "alert_rating_floor",
    "al_health_email", "al_health_sms", "al_health_push", "al_1star_email", "al_1star_sms", "al_1star_push",
    "al_2star_email", "al_2star_sms", "al_2star_push", "al_3star_email", "al_3star_sms", "al_3star_push",
    "al_5star_email", "al_5star_sms", "al_5star_push", "al_spike_email", "al_spike_sms", "al_spike_push",
    "al_unres_email", "al_unres_sms", "al_unres_push",
)
# A login's own notification choices, and what each means with no override.
LOGIN_KEYS = {"push_enabled": True, "push_muted_types": [], "quiet_start": None, "quiet_end": None}
# Alert types no login can mute on their own phone: health and safety, an
# issue assigned to them, the coverage gap they are being asked to fill.
UNMUTABLE_TYPES = frozenset({"health", "issue", "issue_escalated", "coverage", "critical_low"})


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if db_path is None or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_preferences(db_path=None):
    """Boot DDL (models.init_db) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS preferences (
            scope     TEXT NOT NULL,          -- login | location | org
            scope_id  TEXT NOT NULL,          -- "<user_id>:<restaurant_id>" | "<restaurant_id>" | org_key()
            key       TEXT NOT NULL,
            value     TEXT,                   -- JSON
            set_by    INTEGER,
            set_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (scope, scope_id, key)
        )""")
        conn.commit()
    finally:
        conn.close()


def _get(r, k):
    if r is None:
        return None
    if isinstance(r, dict):
        return r.get(k)
    return getattr(r, k, None)


def org_key(restaurant):
    """The organisation a location belongs to: its organization_id, else its
    group name under its owner's email (models.get_location_group's rule);
    None for a location in no group."""
    org = _get(restaurant, "organization_id")
    if org:
        return f"org:{int(org)}"
    group = str(_get(restaurant, "location_group") or "").strip()
    if not group:
        return None
    return f"grp:{str(_get(restaurant, 'owner_email') or '').strip().lower()}|{group}"


def _set(scope, scope_id, key, value, set_by=None, db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO preferences (scope, scope_id, key, value, set_by, set_at) "
                     "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(scope, scope_id, key) DO UPDATE SET "
                     "value=excluded.value, set_by=excluded.set_by, set_at=excluded.set_at",
                     (scope, str(scope_id), key, json.dumps(value, default=str), set_by))
        conn.commit()
    finally:
        conn.close()


def _rows(scope, scope_id, db_path=None) -> dict:
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT key, value, set_by, set_at FROM preferences WHERE scope=? AND scope_id=?",
                                (scope, str(scope_id))).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            v = json.loads(r["value"]) if r["value"] is not None else None
        except (TypeError, ValueError):
            v = None
        out[r["key"]] = {"value": v, "set_by": r["set_by"], "set_at": r["set_at"]}
    return out


# ── the organisation layer ──────────────────────────────────────────────────

def group_locations(restaurant, db_path=None) -> list:
    """Every location in this location's group (itself included), by the
    one group rule (models.get_location_group scoped to its owner)."""
    group = str(_get(restaurant, "location_group") or "").strip()
    if not group:
        return []
    import models
    try:
        return models.get_location_group(group, owner_email=_get(restaurant, "owner_email"),
                                         db_path=db_path or models.DB_PATH)
    except Exception:
        return []


def org_location_ids(restaurant_id, db_path=None) -> list:
    """The ids of the OTHER locations in this location's organisation (same
    org_key: its organization_id, else its group under its owner) — where a
    person's own learning and an owner's organisation-wide facts are read
    from (memory re-audit 9/29/26, PEOPLE-13). [] for a location in no
    organisation."""
    import models
    try:
        r = models.get_restaurant(restaurant_id, db_path) if db_path else models.get_restaurant(restaurant_id)
    except Exception:
        return []
    ok = org_key(r)
    if not ok:
        return []
    ids = set()
    org = _get(r, "organization_id")
    if org:
        conn = get_conn(db_path)
        try:
            ids = {row[0] for row in conn.execute("SELECT id FROM restaurants WHERE organization_id=?",
                                                  (int(org),)).fetchall()}
        except Exception:
            ids = set()
        finally:
            conn.close()
    else:
        ids = {loc["id"] for loc in group_locations(r, db_path=db_path) if loc.get("id") is not None}
    return sorted(i for i in ids if i != restaurant_id)


def may_apply_to_all(user, restaurant) -> bool:
    """A group owner: an account holder who may switch between the group's
    locations (LOCATION_SWITCH), on a location that is in a group."""
    if not user or not org_key(restaurant):
        return False
    try:
        from permissions import LOCATION_SWITCH, has_permission, is_principal
        return bool(user.get("is_admin")) or (is_principal(user) and has_permission(user, LOCATION_SWITCH))
    except Exception:
        return False


def apply_to_all_locations(restaurant, keys, user=None, db_path=None) -> dict:
    """Make this location's values of `keys` (ORG_KEYS only) the
    organisation's default and every location's setting. Returns
    {"keys", "locations": [{id, name}], "skipped": [keys not allowed]}."""
    import models
    keys = [k for k in dict.fromkeys(keys or ()) if k]
    allowed = [k for k in keys if k in ORG_KEYS]
    skipped = [k for k in keys if k not in ORG_KEYS]
    ok = org_key(restaurant)
    if not ok or not allowed:
        return {"keys": [], "locations": [], "skipped": skipped}
    values = {k: _get(restaurant, k) for k in allowed}
    by = (user or {}).get("id")
    for k, v in values.items():
        _set("org", ok, k, v, set_by=by, db_path=db_path)
    changed = []
    for loc in group_locations(restaurant, db_path=db_path):
        if loc["id"] == _get(restaurant, "id"):
            changed.append({"id": loc["id"], "name": loc.get("location_name") or loc.get("name")})
            continue
        fields = {k: v for k, v in values.items() if loc.get(k) != v}
        if fields:
            models.update_restaurant(loc["id"], fields, db_path=db_path or models.DB_PATH)
        changed.append({"id": loc["id"], "name": loc.get("location_name") or loc.get("name")})
    return {"keys": allowed, "locations": changed, "skipped": skipped}


def _product_default(key):
    import dataclasses
    import models
    d = next((f.default for f in dataclasses.fields(models.Restaurant) if f.name == key), None)
    return None if d is dataclasses.MISSING else d


def inherit_org_defaults(restaurant_id, db_path=None) -> dict:
    """A location that joined a group takes the organisation's defaults onto
    the settings it has not made its own — a field still at the product
    default (or empty). A value the location set itself is its override and
    is kept: an owner's edit never vanishes because a group was named.
    Returns the fields written. models.update_restaurant calls it when a
    location's group changes."""
    import models
    r = models.get_restaurant(restaurant_id, db_path=db_path or models.DB_PATH)
    ok = org_key(r)
    if not ok:
        return {}
    defaults = _rows("org", ok, db_path=db_path)
    fields = {}
    for k, d in defaults.items():
        if k not in ORG_KEYS:
            continue
        mine = _get(r, k)
        if mine == d["value"]:
            continue
        if mine in (None, "") or mine == _product_default(k):
            fields[k] = d["value"]
    if fields:
        models.update_restaurant(restaurant_id, fields, db_path=db_path or models.DB_PATH)
    return fields


def org_defaults(restaurant, db_path=None) -> dict:
    ok = org_key(restaurant)
    return _rows("org", ok, db_path=db_path) if ok else {}


# ── the login layer ─────────────────────────────────────────────────────────

def _login_scope(user_id, restaurant_id):
    return f"{int(user_id)}:{int(restaurant_id)}"


def login_overrides(user_id, restaurant_id, db_path=None) -> dict:
    """This login's own choices at this location: {key: value} for the
    LOGIN_KEYS it has set."""
    if user_id is None or restaurant_id is None:
        return {}
    return {k: d["value"] for k, d in _rows("login", _login_scope(user_id, restaurant_id), db_path=db_path).items()
            if k in LOGIN_KEYS}


def _hm(s):
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except (TypeError, ValueError):
        pass
    return None


def set_login_overrides(user_id, restaurant_id, changes, db_path=None) -> dict:
    """Save this login's own notification choices. Raises ValueError for a
    malformed value. Returns the overrides now in force."""
    changes = dict(changes or {})
    clean = {}
    if "push_enabled" in changes:
        clean["push_enabled"] = bool(changes["push_enabled"])
    if "push_muted_types" in changes:
        vals = changes["push_muted_types"]
        if not isinstance(vals, (list, tuple)):
            raise ValueError("push_muted_types must be a list of alert types")
        clean["push_muted_types"] = sorted({str(v).strip()[:40] for v in vals if str(v).strip()}
                                           - UNMUTABLE_TYPES)
    for k in ("quiet_start", "quiet_end"):
        if k in changes:
            v = changes[k]
            if v in (None, ""):
                clean[k] = None
            elif _hm(v) is None:
                raise ValueError(f"{k} must be HH:MM")
            else:
                clean[k] = f"{_hm(v) // 60:02d}:{_hm(v) % 60:02d}"
    for k, v in clean.items():
        _set("login", _login_scope(user_id, restaurant_id), k, v, set_by=user_id, db_path=db_path)
    return login_overrides(user_id, restaurant_id, db_path=db_path)


def _in_window(now_min, start, end):
    s, e = _hm(start), _hm(end)
    if s is None or e is None or s == e:
        return False
    return s <= now_min < e if s < e else (now_min >= s or now_min < e)


def push_allowed(user_id, restaurant_id, alert_type, now_local=None, db_path=None, _cache=None) -> bool:
    """Whether this login's OWN choices let `alert_type` reach their phone
    now. Only ever takes away: the location's matrix, quiet hours and cap
    were applied before the push was raised. Health, safety and an issue
    assigned to them always reach them."""
    if user_id is None or restaurant_id is None:
        return True
    if str(alert_type or "") in UNMUTABLE_TYPES:
        return True
    key = (user_id, restaurant_id)
    if _cache is not None and key in _cache:
        o = _cache[key]
    else:
        o = login_overrides(user_id, restaurant_id, db_path=db_path)
        if _cache is not None:
            _cache[key] = o
    if not o:
        return True
    if o.get("push_enabled") is False:
        return False
    if str(alert_type or "") in set(o.get("push_muted_types") or ()):
        return False
    if o.get("quiet_start") and o.get("quiet_end"):
        if now_local is None:
            try:
                from time_utils import restaurant_now_by_id
                now_local = restaurant_now_by_id(restaurant_id, naive=True)
            except Exception:
                now_local = datetime.now()
        if _in_window(now_local.hour * 60 + now_local.minute, o["quiet_start"], o["quiet_end"]):
            return False
    return True


# ── one read, with where it came from ───────────────────────────────────────

def resolve(key, restaurant, user=None, db_path=None) -> dict:
    """{"value", "source", "set_at"} for one preference: a LOGIN_KEYS key
    from this login's override ("you") or the product default; a location
    key from its column — "all locations" when it matches the
    organisation's default, "this location" when it differs from it (or
    from the product default), else "default"."""
    rid = _get(restaurant, "id")
    if key in LOGIN_KEYS:
        o = login_overrides((user or {}).get("id"), rid, db_path=db_path) if user else {}
        if key in o:
            return {"value": o[key], "source": "you", "set_at": None}
        return {"value": LOGIN_KEYS[key], "source": "default", "set_at": None}
    value = _get(restaurant, key)
    org = org_defaults(restaurant, db_path=db_path).get(key)
    if org is not None and org.get("value") == value:
        return {"value": value, "source": "all locations", "set_at": org.get("set_at")}
    if org is not None:
        return {"value": value, "source": "this location", "set_at": None}
    default = _product_default(key)
    return {"value": value, "source": "default" if value in (None, "") or value == default else "this location",
            "set_at": None}


# ── engagement, per login ───────────────────────────────────────────────────

ENGAGEMENT_MIN_DELIVERED = 10


def engagement_for_login(user_id, restaurant_id, days=60, db_path=None) -> list:
    """[{alert_type, delivered, opened}] for ONE login: pushes delivered to
    their own devices and the opens they made. The restaurant-wide sum hid
    that the owner never opens what a manager opens every time."""
    since = f"-{int(days)} days"
    try:
        conn = get_conn(db_path)
        try:
            # One notification per minute per type: a login with two phones
            # is sent the same push twice, and it is still one notification.
            sent = {r["alert_type"]: r["n"] for r in conn.execute(
                "SELECT pd.alert_type, COUNT(DISTINCT strftime('%Y-%m-%d %H:%M', pd.created_at)) AS n "
                "FROM push_deliveries pd JOIN device_tokens d ON d.id = pd.device_token_id "
                "WHERE pd.restaurant_id=? AND d.user_id=? AND pd.ok=1 AND pd.created_at >= datetime('now', ?) "
                "GROUP BY pd.alert_type", (restaurant_id, user_id, since)).fetchall()}
            opened = {r["alert_type"]: r["n"] for r in conn.execute(
                "SELECT alert_type, COUNT(*) AS n FROM notification_opens WHERE restaurant_id=? AND user_id=? "
                "AND opened_at >= datetime('now', ?) GROUP BY alert_type", (restaurant_id, user_id, since)).fetchall()}
        finally:
            conn.close()
    except Exception as e:
        log.debug("preferences: engagement unreadable for %s/%s: %s", user_id, restaurant_id, e)
        return []
    rows = [{"alert_type": t, "delivered": n, "opened": opened.get(t, 0)} for t, n in sent.items()]
    rows.sort(key=lambda r: -r["delivered"])
    return rows


def never_opened_for_login(user_id, restaurant_id, db_path=None) -> list:
    """The alert types this login receives a lot of on their phone and never
    opens — the raw material for "mute these on your phone?" (their own
    override, not the restaurant's setting)."""
    out = []
    for r in engagement_for_login(user_id, restaurant_id, db_path=db_path):
        if r["alert_type"] in UNMUTABLE_TYPES or r["opened"] or r["delivered"] < ENGAGEMENT_MIN_DELIVERED:
            continue
        out.append(dict(r, days=60))
    return out
