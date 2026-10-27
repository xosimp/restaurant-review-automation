"""Cohort rollout flags: turn a change on for some restaurants first
(audit #25, roadmap P3).

Every change reached every restaurant at once; the only switches were
env-wide kill switches (docs/ops/ENVIRONMENT.md, "Rollback and kill
switches"). `enabled(feature, restaurant_id)` answers whether one restaurant
is in a feature's cohort:

1. the deny list wins, then the allow list;
2. otherwise the restaurant is in when its stable bucket — sha256 of
   "<feature>:<restaurant_id>", 0..99 — is under the feature's percentage.
   The bucket is stable across processes and deploys, and different per
   feature, so 10% of one feature is not the same 10% as another's; raising
   the percentage only ever adds restaurants.

A feature's state comes from, in order:

- the environment, `ROLLOUT_<FEATURE>` (upper-cased, non-alphanumerics as
  `_`): `on` / `off`, or `<percent>[;allow=1,2][;deny=3]`. Deploy-time, and it
  WINS over the table: `ROLLOUT_X=off` is a kill switch nothing at run time
  can override;
- the `rollout_flags` table (one row per feature: percent, allow and deny
  lists, note), set from the admin console (`POST /admin/api/rollout`,
  audited through `admin_events.record_admin_action`) — run time, no deploy;
- otherwise `default` (False unless the caller says).

No restaurant id (a platform job) is in only at 100%. Reads are cached for
`ROLLOUT_CACHE_SECONDS` per process (a set() here drops this process's copy;
another process sees it within the window). The table is created at boot
(`init_rollout`, from models.init_db).

FEATURES lists every feature the code consults, with the default it keeps
when nothing sets it (blind re-audit 10/27/26 RF6 — the module gated
nothing). A risky new behaviour ships behind one (ARCHITECTURE_MANIFEST.md
→ Release compatibility): default OFF and 0% → an allow-list → 10% → 100%
for a new owner-visible behaviour, or default ON with the flag as a run-time
kill switch for a change that must reach everyone. The console lists them
(GET /admin/api/rollout, `known`).
"""
import hashlib
import json
import os
import re
import threading
import time

import models as _models_mod
import process_caches as _process_caches


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md's bound-import hazard)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


ROLLOUT_CACHE_SECONDS = 30
_FEATURE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DDL = (
    """CREATE TABLE IF NOT EXISTS rollout_flags (
        feature     TEXT PRIMARY KEY,
        percent     INTEGER NOT NULL DEFAULT 0,
        allow_json  TEXT NOT NULL DEFAULT '[]',
        deny_json   TEXT NOT NULL DEFAULT '[]',
        note        TEXT,
        updated_by  TEXT,
        updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
)

_flag_cache = {}          # (db_path, feature) -> (read_at, state or None)
_process_caches.register("rollout._flag_cache", _flag_cache)
_LOCK = threading.Lock()


# Every feature consulted through enabled(), its default and where.
FEATURES = {
    "client_min_build": {
        "default": True, "where": "client_compat.refusal_allowed",
        "what": "an iPhone build under MOBILE_MIN_BUILD is refused with 426 (A06-02)"},
    "client_headerless_cutoff": {
        "default": True, "where": "client_compat.refusal_allowed",
        "what": "a request with no X-Cavnar-App header after MOBILE_HEADERLESS_CUTOFF is refused with 426"},
    "stale_tab_reload": {
        "default": True, "where": "client_compat.page_build",
        "what": "the web dashboard's 'Cavnar AI was updated' bar after a deploy (A06-13)"},
    "staff_docs_plan_gate": {
        "default": True, "where": "auth._staff_knowledge_gate_off",
        "what": "the staff brief, house rules, staff docs and certifications held to the Labor plan and role "
                "gates (blind re-audit B3); off, only each route's own role check"},
}


class RolloutError(ValueError):
    """A feature name or a setting that cannot be stored."""


def init_rollout(db_path=None):
    """The rollout table — at boot (models.init_db), never on a call."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def bucket(feature, restaurant_id) -> int:
    """0..99, stable for (feature, restaurant)."""
    digest = hashlib.sha256(f"{feature}:{int(restaurant_id)}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def _ids(values):
    out = []
    for v in values or ():
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            raise RolloutError(f"{v!r} is not a restaurant id")
    return sorted(set(out))


def _state(percent=0, allow=(), deny=(), source="table", note=None):
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        raise RolloutError("percent should be a whole number from 0 to 100")
    if not 0 <= pct <= 100:
        raise RolloutError("percent should be a whole number from 0 to 100")
    return {"percent": pct, "allow": _ids(allow), "deny": _ids(deny), "source": source, "note": note}


def env_name(feature) -> str:
    return "ROLLOUT_" + re.sub(r"[^A-Za-z0-9]", "_", str(feature)).upper()


def parse_env(value):
    """The state an env value names, or None for an empty one. `on`/`off`,
    or `<percent>[;allow=1,2][;deny=3]`. A malformed value is OFF (and says
    so): a typo must not roll a feature out to everyone."""
    raw = (value or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low in ("on", "all", "true", "1", "yes"):
        return _state(100, source="env")
    if low in ("off", "none", "false", "0", "no"):
        return _state(0, source="env")
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    try:
        pct, allow, deny = int(parts[0].rstrip("%")), [], []
        for p in parts[1:]:
            key, _, ids = p.partition("=")
            listed = [i for i in ids.split(",") if i.strip()]
            if key.strip().lower() == "allow":
                allow = listed
            elif key.strip().lower() == "deny":
                deny = listed
            else:
                raise RolloutError(f"unknown part {p!r}")
        return _state(pct, allow, deny, source="env")
    except (ValueError, IndexError) as e:
        return dict(_state(0, source="env"), error=f"unreadable {raw!r}: {e}")


def _read_table(feature, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT percent, allow_json, deny_json, note FROM rollout_flags WHERE feature=?",
                           (feature,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return _state(row["percent"], json.loads(row["allow_json"] or "[]"), json.loads(row["deny_json"] or "[]"),
                  source="table", note=row["note"])


def state(feature, db_path=None):
    """The feature's effective state {percent, allow, deny, source, note}, or
    None when nothing sets it. The environment wins over the table."""
    env = parse_env(os.environ.get(env_name(feature)))
    if env is not None:
        return env
    key = (db_path, feature)
    now = time.monotonic()
    with _LOCK:
        hit = _flag_cache.get(key)
    if hit is not None and now - hit[0] < ROLLOUT_CACHE_SECONDS:
        return hit[1]
    st = _read_table(feature, db_path=db_path)
    with _LOCK:
        _flag_cache[key] = (now, st)
    return st


def decide(st, feature, restaurant_id) -> bool:
    """Whether `restaurant_id` is in the cohort `st` describes."""
    if restaurant_id is None:
        return st["percent"] >= 100
    rid = int(restaurant_id)
    if rid in st["deny"]:
        return False
    if rid in st["allow"]:
        return True
    return bucket(feature, rid) < st["percent"]


def enabled(feature, restaurant_id=None, default=False, db_path=None) -> bool:
    """Is this restaurant in `feature`'s cohort? `default` when nothing sets
    the feature, or when its state cannot be read (a rollout must never be
    what takes a request down — the read failure is ops.capture-d)."""
    try:
        st = state(feature, db_path=db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="rollout_read", context=f"feature={feature} restaurant_id={restaurant_id}",
                    restaurant_id=restaurant_id)
        return bool(default)
    if st is None:
        return bool(default)
    return decide(st, feature, restaurant_id)


def set_flag(feature, percent, allow=(), deny=(), note=None, by=None, db_path=None):
    """Store a feature's cohort; returns (before, after) states. Validates
    everything before writing."""
    feature = str(feature or "").strip().lower()
    if not _FEATURE.match(feature):
        raise RolloutError("a feature is lower_snake_case, 2 to 64 characters")
    after = _state(percent, allow, deny, note=(str(note)[:300] if note else None))
    before = _read_table(feature, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO rollout_flags (feature, percent, allow_json, deny_json, note, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(feature) DO UPDATE SET percent=excluded.percent, allow_json=excluded.allow_json, "
            "deny_json=excluded.deny_json, note=excluded.note, updated_by=excluded.updated_by, "
            "updated_at=CURRENT_TIMESTAMP",
            (feature, after["percent"], json.dumps(after["allow"]), json.dumps(after["deny"]), after["note"],
             (str(by)[:120] if by else None)))
        conn.commit()
    finally:
        conn.close()
    with _LOCK:
        for key in [k for k in _flag_cache if k[1] == feature]:
            _flag_cache.pop(key, None)
    return before, after


def list_flags(db_path=None):
    """Every stored feature with its state, and any ROLLOUT_* env override."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT feature, percent, allow_json, deny_json, note, updated_by, updated_at "
                            "FROM rollout_flags ORDER BY feature").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        env = parse_env(os.environ.get(env_name(r["feature"])))
        out.append({"feature": r["feature"], "percent": int(r["percent"]),
                    "allow": json.loads(r["allow_json"] or "[]"), "deny": json.loads(r["deny_json"] or "[]"),
                    "note": r["note"], "updated_by": r["updated_by"], "updated_at": r["updated_at"],
                    "env_override": env})
    return out


def known_features(db_path=None):
    """FEATURES with each one's environment variable and effective state
    (None: nothing sets it, so its default holds) — the console's list."""
    out = []
    for name, info in sorted(FEATURES.items()):
        try:
            st = state(name, db_path=db_path)
        except Exception as e:
            st = {"error": f"unreadable: {e}"[:200]}
        out.append({"feature": name, "default": bool(info["default"]), "where": info["where"],
                    "what": info["what"], "env": env_name(name), "state": st})
    return out
