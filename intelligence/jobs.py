"""The two nightly passes.

`run_features` walks every active restaurant and writes this ISO week's
feature row — bounded by a wall clock and resumed from a cursor in
`job_cursors`, so a pass that runs out of time tonight picks up where it
stopped tomorrow rather than starving the same tail every night
(run_daily_fetch's rule).

`run_learning` reads only the materialized tables: feedback sync, pattern
discovery, benchmarks over each restaurant's peer PARTITION (the owner-
confirmed profile, counted by organisation), the peer assignment ledger,
the balanced-panel cohort series, the confidence log.
"""
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import models as _models_mod
from models import DB_PATH, get_all_restaurants
from . import categories, feedback, patterns, benchmarks, scoring, privacy
from . import features as _features
from . import metrics_registry as _reg


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


CURSOR_KEY = "intelligence_features"
FEATURE_WALL_SECONDS = 240
FEATURE_WORKERS = 4

_LIVE = ("trial", "active")


def _cursor_get(db_path):
    conn = get_conn(db_path)
    try:
        # job_cursors is created by models.init_db (one owner, one definition).
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
    finally:
        conn.close()
    try:
        return int(row["value"]) if row and row["value"] else 0
    except (TypeError, ValueError):
        return 0


def _cursor_set(db_path, value):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                     (CURSOR_KEY, str(int(value))))
        conn.commit()
    finally:
        conn.close()


# After is_demo is turned off, the seeded history is still in the tables
# (never hard-deleted). The longest window a feature reads is 90 days
# (features.compute), so the restaurant stays out of cross-restaurant
# learning until that window holds none of it.
SEEDED_HISTORY_DAYS = 90

# The one predicate (one parameter: f"-{SEEDED_HISTORY_DAYS} days"), so SQL
# readers such as scoring.kind_stats filter exactly as real_restaurant_ids.
# A test or internal account flagged `exclude_from_learning` (Benchmarking
# audit #9) is out of every cross-restaurant figure exactly as a demo is.
REAL_RESTAURANT_SQL = ("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=0 AND "
                       "COALESCE(exclude_from_learning,0)=0 AND "
                       "(demo_cleared_at IS NULL OR demo_cleared_at < datetime('now', ?))")
# Its complement over the restaurants table — what readers of other tables
# exclude (a row whose restaurant is not in the table is left alone).
SEEDED_RESTAURANT_SQL = ("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1 OR "
                         "COALESCE(exclude_from_learning,0)=1 OR "
                         "(demo_cleared_at IS NOT NULL AND demo_cleared_at >= datetime('now', ?))")


def _schema_gap(err) -> bool:
    text = str(err).lower()
    return "no such column" in text or "no such table" in text


def seeded_restaurant_ids(db_path=DB_PATH) -> set:
    """The complement of real_restaurant_ids within the restaurants table:
    demo accounts, recently de-flagged ones, and — since the memory
    re-audit (9/29/26, PLATFORM-14) — every restaurant the one "may teach"
    predicate refuses (models.learning_exclusion: an admin's exclude
    override, internal billing, a home of internal logins only, a test
    name). This was a second, narrower definition (is_demo,
    exclude_from_learning, demo_cleared_at), so active_restaurants() handed
    the band, pattern, ledger and prediction passes test and internal
    accounts. Readers of the feature and event tables drop these ids."""
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(SEEDED_RESTAURANT_SQL, (f"-{SEEDED_HISTORY_DAYS} days",)).fetchall()
        except Exception as e:
            if not _schema_gap(e):
                raise
            rows = conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1").fetchall()
        out = {int(r["id"]) for r in rows}
        out |= _models_mod.learning_ineligible_ids(conn=conn)
    finally:
        conn.close()
    return out


def real_restaurant_ids(db_path=DB_PATH) -> set:
    """Ids of restaurants whose data may feed CROSS-restaurant learning —
    benchmarks, patterns, trends, cohort and platform rates, the privacy
    floor's count (CA3 F7). Excluded: is_demo=1 accounts (seeded, synthetic),
    and a restaurant whose is_demo flag was turned off less than
    SEEDED_HISTORY_DAYS ago (restaurants.demo_cleared_at), because its
    windows still hold the seeded rows. A restaurant's OWN screens are not
    filtered by this — only what it contributes to everyone else's."""
    conn = get_conn(db_path)
    try:
        ids = {int(r["id"]) for r in conn.execute("SELECT id FROM restaurants").fetchall()}
    finally:
        conn.close()
    # The one predicate (PLATFORM-14): every restaurant not seeded_restaurant_ids.
    return ids - seeded_restaurant_ids(db_path)


# models.learning_eligible over every restaurant, kept this long per
# database — kind_stats asks per kind on a Home build.
ELIGIBILITY_TTL_SECONDS = 60
_excluded_cache = {"key": None, "at": 0.0, "ids": None}


def excluded_learning_ids(db_path=DB_PATH) -> set:
    """Restaurants whose rows may teach NO cross-restaurant figure (memory
    audit 9/29/26 — "every learner filters with models.learning_eligible"):
    every restaurant models.learning_eligible refuses (a demo, a test or
    internal account — exclude_from_learning, billing 'internal') and every
    one still inside its seeded quarantine (seeded_restaurant_ids). A
    deleted restaurant has no row, so its kept, anonymised rows are not
    excluded: they count toward groups and never carry a name. Cached
    briefly per database."""
    import time as _time
    key = (db_path or DB_PATH, getattr(_models_mod, "DB_PATH", None), id(getattr(_models_mod, "get_conn", None)))
    now = _time.monotonic()
    if _excluded_cache["key"] == key and _excluded_cache["ids"] is not None \
            and now - _excluded_cache["at"] < ELIGIBILITY_TTL_SECONDS:
        return _excluded_cache["ids"]
    conn = get_conn(db_path)
    try:
        try:
            out = {int(r["id"]) for r in conn.execute(SEEDED_RESTAURANT_SQL, (f"-{SEEDED_HISTORY_DAYS} days",))}
        except Exception as e:
            if not _schema_gap(e):
                raise
            out = {int(r["id"]) for r in conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1")}
        rows = conn.execute("SELECT * FROM restaurants").fetchall()
        homes = _models_mod._internal_home_ids_on(conn)
    finally:
        conn.close()
    since = {}
    for r in rows:
        # Fails closed (memory re-audit 9/29/26, PLATFORM-11): a row whose
        # eligibility cannot be read teaches nothing. It read as eligible.
        try:
            ok = _models_mod.learning_exclusion(dict(r), internal_homes=homes) is None
        except Exception:
            ok = False
        if not ok:
            out.add(int(r["id"]))
        if "learning_since" in r.keys() and r["learning_since"]:
            since[int(r["id"])] = str(r["learning_since"])
    _excluded_cache.update(key=key, at=now, ids=out, since=since)
    return out


def invalidate_excluded(*_a):
    _excluded_cache.update(key=None, at=0.0, ids=None, since=None)


def learning_since_by_id(db_path=DB_PATH) -> dict:
    """{restaurant_id: learning_since} — a converted demo's first day of real
    learning (models.learning_since, stamped when is_demo is turned off).
    A cross-restaurant reader drops every row recorded before it
    (before_learning), on top of the whole-restaurant exclusion above.
    Cached with excluded_learning_ids (memory fix round INT #20)."""
    excluded_learning_ids(db_path=db_path)
    return dict(_excluded_cache.get("since") or {})


def before_learning(restaurant_id, when, since=None) -> bool:
    """Whether a row stamped `when` (an ISO date or stamp) was recorded
    before its restaurant's learning_since — its demo era, which teaches
    no cross-restaurant figure. `since`: learning_since_by_id()."""
    ls = (since or {}).get(int(restaurant_id)) if restaurant_id is not None else None
    if not ls or not when:
        return False
    # Both sides as "YYYY-MM-DD HH:MM:SS" (memory re-audit 9/29/26,
    # PLATFORM-17): an ISO "T" stamp compared later than the same morning's
    # space-separated learning_since ('T' > ' '), so that morning's demo-era
    # rows taught.
    return _stamp19(when) < _stamp19(ls)


def _stamp19(v) -> str:
    return str(v).replace("T", " ")[:19]


def learning_since_week(restaurant_id, since=None):
    """The ISO week ("2026-W40") of a restaurant's learning_since, or None:
    a feature or A/B week before it is its demo era."""
    ls = (since or {}).get(int(restaurant_id)) if restaurant_id is not None else None
    if not ls:
        return None
    try:
        y, w, _ = date.fromisoformat(str(ls)[:10]).isocalendar()
        return f"{y}-W{w:02d}"
    except ValueError:
        return None


def learning_labels(db_path=DB_PATH, ids=None, conn=None) -> dict:
    """{restaurant_id: {cohort, partitions: {family: key}, google}} for every
    restaurant (or `ids`) — what feedback.sync stamps each row with: the
    owner-confirmed concept (categories.confirmed_type — None for a guess),
    the confirmed partition per metric family (categories.partition_key),
    and whether its reviews come through the owner's Google connection
    (provenance.google_connected_ids). Every restaurant, seeded and excluded
    ones too: a label is a fact about the restaurant, eligibility is the
    readers' (PLATFORM-15). Read on the caller's connection when given (the
    sync writes a whole pass on one)."""
    from . import provenance
    own = conn is None
    conn = conn or get_conn(db_path)
    try:
        if ids is not None:
            want = sorted({int(i) for i in ids})
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM restaurants WHERE id IN ({','.join('?' for _ in want)})", want).fetchall()] \
                if want else []
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM restaurants").fetchall()]
        google = provenance.google_connected_ids(conn=conn)
    finally:
        if own:
            conn.close()
    out = {}
    for d in rows:
        r = SimpleNamespace(**d)
        prof = categories.profile_for(r)
        out[int(d["id"])] = {"cohort": categories.confirmed_type(r),
                             "partitions": {fam: categories.partition_key(prof, fam) for fam in categories.FAMILIES},
                             "google": int(d["id"]) in google}
    return out


def active_restaurants(db_path=DB_PATH, include_demo=False) -> list:
    """Restaurants in service, for the nightly passes. Cross-restaurant
    learning (cohorts, patterns, benchmarks, the confidence log) takes the
    default — real restaurants only (real_restaurant_ids). run_features
    passes include_demo=True: a demo account's OWN feature row still backs
    its own screens; the cross-restaurant readers leave it out."""
    live = [r for r in get_all_restaurants(db_path)
            if (getattr(r, "billing_status", None) or "trial").lower() in _LIVE]
    if include_demo:
        return live
    seeded = seeded_restaurant_ids(db_path)
    return [r for r in live if r.id not in seeded]


def cohorts_for(restaurants) -> dict:
    """{restaurant_id: category or None} — the type cohort map patterns,
    feedback and the confidence log take, resolved once per night. Only a
    type the owner (or admin) SET: a type Cavnar guessed from the name never
    makes a restaurant a member of a group, and never counts toward a floor
    (Benchmarking audit #8, BM1-3)."""
    out = {}
    for r in restaurants:
        cat, src = categories.category_for(r)
        out[r.id] = cat if src == "set" else None
    return out


# ── who may stand in a peer band (Benchmarking audit #9, #39) ─────────────
# A band is only as good as its members: a trial running on sample or
# default inputs, a half-connected account and a test account all set
# other restaurants' ranges (BM1-24). Eligibility, per member:
MIN_LIVE_WEEKS = 8               # weeks since signup
MIN_MEMBER_COMPLETENESS = 0.5    # share of the measures on file (features.completeness)


def _weeks_since(raw, today):
    try:
        d = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (today - d).days / 7.0)


# A member's labor cost is its own when at least this share of its hours is
# priced at rates the owner set (fix round #11 via workstream A: one priced
# role over a $26 default for everyone else is not a real labor cost).
# The same rule as engine.labor_cost_sourced (the viewer's side), applied to
# a member row read here without a Restaurant object.
LABOR_SOURCED_SHARE = 0.8


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _labor_cost_sourced(d, db_path=DB_PATH) -> bool:
    """Whether restaurant row `d`'s labor cost rests on rates its owner set
    for at least LABOR_SOURCED_SHARE of its hours. A fallback rate the owner
    set (role_rates `_default`, or an hourly_rate that is not the $26
    default) prices every unpriced hour, so it counts in full; otherwise the
    share is the hours of the priced roles (matched case- and space-
    insensitively, as labor._shift_rate does) over all hours in its shifts."""
    import thresholds as _thr
    if _thr.labor_cost_basis(d) == "default":
        return False
    default = float(_thr.LABOR_DEFAULT_HOURLY_RATE)

    def owner_set(v):
        f = _float(v)
        return f is not None and f > 0 and abs(f - default) > 1e-9
    try:
        rates = json.loads(d.get("role_rates_json") or "{}") if isinstance(d.get("role_rates_json"), str) \
            else dict(d.get("role_rates_json") or {})
    except Exception:
        rates = {}
    if owner_set(rates.get("_default")) or owner_set(d.get("hourly_rate")):
        return True
    priced = {" ".join(str(k).split()).lower() for k, v in rates.items()
              if k != "_default" and (_float(v) or 0) > 0}
    if not priced:
        return False
    try:
        cd = _models_mod.get_client_data(int(d["id"]), db_path) if db_path != DB_PATH \
            else _models_mod.get_client_data(int(d["id"]))
    except Exception:
        cd = None
    raw = (cd or {}).get("shifts_csv")
    if not raw:
        return False
    import csv
    import io
    total = covered = 0.0
    for row in csv.DictReader(io.StringIO(str(raw).lstrip("﻿"))):
        low = {str(k or "").strip().lower(): v for k, v in row.items()}
        h = _float(low.get("actual_hours"))
        if h is None:
            h = _float(low.get("scheduled_hours"))
        if not h or h <= 0:
            continue
        total += h
        if " ".join(str(low.get("role") or "").split()).lower() in priced:
            covered += h
    return total > 0 and covered / total >= LABOR_SOURCED_SHARE


def member_info(db_path=DB_PATH, today: date = None) -> dict:
    """{restaurant_id: {org, org_hash, org_hashes, place_id, excluded,
    live, live_weeks, cost_basis, profile, category, type_source}} for every
    restaurant row — what the band builders need to count organisations,
    merge duplicate listings and decide eligibility. Server-side only; never
    a payload.

    `org` is privacy.org_map's organisation (organisation, owner email,
    shared owner logins, Stripe customer — R1-01); `org_hashes` adds every
    key its restaurants were stored under before, so the ledger can take a
    restaurant's organisation out of a band frozen earlier in the week."""
    import thresholds as _thr
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM restaurants").fetchall()
        homes = _models_mod._internal_home_ids_on(conn)
    finally:
        conn.close()
    try:
        orgs = privacy.org_map(db_path=db_path)
    except Exception as e:
        print(f"[intelligence] organisation map unavailable, falling back to each row's key: {e}")
        orgs = {}
    dicts = [dict(r) for r in rows]
    aliases = {}
    for d in dicts:
        org = orgs.get(int(d["id"])) or privacy.org_key(d)
        aliases.setdefault(org, set()).update((org, privacy.org_key(d), privacy.legacy_org_key(d)))
    out = {}
    for d in dicts:
        ns = SimpleNamespace(**d)
        cat, src = categories.category_for(ns)
        org = orgs.get(int(d["id"])) or privacy.org_key(d)
        out[int(d["id"])] = {
            "org": org, "org_hash": privacy.org_hash(org),
            "org_hashes": frozenset(privacy.org_hash(k) for k in aliases.get(org, {org})),
            "place_id": (str(d.get("google_place_id") or "").strip() or None),
            # The one "may teach" predicate (PLATFORM-14): it read
            # exclude_from_learning alone, ignoring the admin's override,
            # internal billing, an internal-only home and a test name.
            "excluded": _models_mod.learning_exclusion(d, internal_homes=homes) is not None,
            "live": (d.get("billing_status") or "trial").lower() in _LIVE,
            "live_weeks": _weeks_since(d.get("created_at"), today),
            "cost_basis": _thr.labor_cost_basis(d),
            "labor_cost_sourced": _labor_cost_sourced(d, db_path=db_path),
            "profile": categories.profile_for(ns),
            "category": cat if src == "set" else None, "type_source": src,
        }
    return out


def ineligible(member, row) -> str | None:
    """Why a restaurant may not stand in any band, or None."""
    if not member:
        return None
    if member.get("excluded"):
        return "excluded from learning (test or internal account)"
    # A cancelled or lapsed customer leaves the bands the night it lapses,
    # not three weeks later when its last feature row ages out (R1-21/R2-23).
    if member.get("live") is False:
        return "no longer an active customer"
    if (member.get("live_weeks") or 0) < MIN_LIVE_WEEKS:
        return f"fewer than {MIN_LIVE_WEEKS} weeks live on Cavnar AI"
    try:
        comp = float((row or {}).get("completeness"))
    except (TypeError, ValueError):
        comp = None
    if comp is not None and comp < MIN_MEMBER_COMPLETENESS:
        return "less than half of its measures on file"
    return None


def eligible_members(latest, members) -> tuple[dict, dict]:
    """({restaurant_id: member}, {reason: count}) — the restaurants that may
    stand in any cross-restaurant group: not ineligible(), and one per
    Google listing (a duplicate listing is the same business twice). The
    one membership rule for bands and patterns (Benchmarking re-audit #21,
    R1-09): a restaurant with no member row is taken as its own
    organisation. Pure over its inputs."""
    elig, seen_place, skipped = {}, {}, {}
    for rid in sorted(latest or {}):
        m = (members or {}).get(rid) or {"org": f"r{rid}", "org_hash": privacy.org_hash(f"r{rid}")}
        why = ineligible(m, (latest or {})[rid]) if (members or {}).get(rid) else None
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        pid = m.get("place_id")
        if pid:
            if pid in seen_place:
                skipped["duplicate listing"] = skipped.get("duplicate listing", 0) + 1
                continue
            seen_place[pid] = rid
        elig[rid] = m
    return elig, skipped


# ── the peer partition and measured drift (Benchmarking audit #20, #30) ───
# The partition is the owner-CONFIRMED profile, and only the owner moves it.
# A measured signal that disagrees with it (alcohol share against bar-led)
# for DRIFT_WEEKS consecutive ISO weeks becomes a question in Account →
# Restaurant profile ("compare you with bar-led restaurants?") — fix round
# #38, R2-13: the old write-side move put the restaurant in a band its own
# card and profile did not name, on three non-consecutive earlier weeks.
DRIFT_WEEKS = 4
BAR_LED_SHARE = 0.40      # alcohol share at which a restaurant runs as bar-led
_DRIFT_MARGIN = 0.05      # hysteresis band around it


def _measured_drift(profile, feats) -> str | None:
    """'bar_led' / 'not_bar_led' when this week's measured alcohol share
    contradicts the confirmed profile, else None."""
    share = (feats or {}).get("alcohol_share")
    if share is None or not profile or not profile.get("confirmed"):
        return None
    bar = bool(profile.get("bar_led"))
    if not bar and share >= BAR_LED_SHARE + _DRIFT_MARGIN:
        return "bar_led"
    if bar and profile.get("service_model") != "bar_led" and share <= BAR_LED_SHARE - _DRIFT_MARGIN:
        return "not_bar_led"
    return None


def _previous_drift(db_path, today, restaurant_id=None) -> dict:
    """{restaurant_id: [(week, drift) of each earlier week, newest first]}."""
    week = _features.iso_week(today)
    conn = get_conn(db_path)
    try:
        sql = "SELECT restaurant_id, week, drift FROM intel_peer_assignments WHERE family='labor' AND week < ?"
        args = [week]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(int(restaurant_id))
        rows = conn.execute(sql + " ORDER BY week DESC", args).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(int(r["restaurant_id"]), []).append((r["week"], r["drift"]))
    return out


def _prev_week(week):
    mon = benchmarks._week_monday(week)
    from datetime import timedelta
    return _features.iso_week(mon - timedelta(weeks=1)) if mon else None


def drift_run(drift, week, history) -> int:
    """How many CONSECUTIVE ISO weeks, ending at `week`, measured the same
    drift: this week's plus the unbroken run of ledger weeks right before it
    (`history` = [(week, drift)], newest first). A gap ends the run."""
    if not drift:
        return 0
    run, expect = 1, _prev_week(week)
    for wk, d in history or ():
        if wk != expect or d != drift:
            break
        run += 1
        expect = _prev_week(wk)
    return run


def peer_partitions(members, latest=None, db_path=DB_PATH, today: date = None) -> dict:
    """{restaurant_id: {family: partition key or None, "staff": key,
    "_ladder": {family: [key, …]}, "_drift": str|None, "_drift_weeks": int}}
    for every member: the profile's own partition per metric family
    (categories.partition_key, the confirmed profile only — measured drift
    never moves it, #38) and the ladder of groups it stands in, finest →
    coarsest (categories.partition_ladder over its measured coordinates,
    #34/#36)."""
    today = today or date.today()
    week = _features.iso_week(today)
    latest = latest if latest is not None else _features.latest_by_restaurant(db_path=db_path)
    prev = _previous_drift(db_path, today)
    out = {}
    for rid, m in (members or {}).items():
        prof = dict(m.get("profile") or {})
        feats = ((latest or {}).get(rid) or {}).get("features") or {}
        drift = _measured_drift(prof, feats)
        vb = feats.get("volume_band")
        out[rid] = {fam: categories.partition_key(prof, fam) for fam in categories.FAMILIES}
        out[rid]["staff"] = categories.partition_key(prof, "staff", volume_band=vb)
        out[rid]["_ladder"] = {fam: categories.partition_ladder(prof, fam, feats)
                               for fam in categories.FAMILIES + ("staff",)}
        out[rid]["_drift"] = drift
        out[rid]["_drift_weeks"] = drift_run(drift, week, prev.get(rid))
    return out


def profile_review(restaurant_id, restaurant=None, db_path=DB_PATH, today: date = None) -> dict | None:
    """The "is your profile still right?" prompt for Account → Restaurant
    profile (categories.profile_review): measured drift over DRIFT_WEEKS
    consecutive weeks, a measured format that contradicts the profile, or a
    confirmation over a year old. None when there is nothing to ask."""
    today = today or date.today()
    if restaurant is None:
        restaurant = _models_mod.get_restaurant(restaurant_id, db_path) if db_path != DB_PATH \
            else _models_mod.get_restaurant(restaurant_id)
    prof = categories.profile_for(restaurant) if restaurant is not None else None
    if not prof or not prof.get("confirmed"):
        return None
    try:
        row = _features.latest(restaurant_id, db_path=db_path) or {}
    except Exception:
        row = {}
    feats = row.get("features") or {}
    drift_info = None
    drift = _measured_drift(prof, feats)
    if drift:
        wk = row.get("week") or _features.iso_week(today)
        hist = _previous_drift(db_path, benchmarks._week_monday(wk) or today, restaurant_id).get(int(restaurant_id))
        weeks = drift_run(drift, wk, hist)
        if weeks >= DRIFT_WEEKS:
            drift_info = {"direction": drift, "weeks": weeks, "share": feats.get("alcohol_share")}
    return categories.profile_review(prof, drift=drift_info, structural=feats, today=today)


_RUNG_ORDER = {"self": 0, "published": 1, "platform": 2, "peers": 3}
_FAMILY_METRICS = {"format": [m for m in _features.BENCHMARK_KEYS if _reg.partition_family(m) == "format"],
                   "labor": [m for m in _features.BENCHMARK_KEYS if _reg.partition_family(m) == "labor"],
                   "food": [m for m in _features.BENCHMARK_KEYS if _reg.partition_family(m) == "food"]}
_FAMILY_INDUSTRY = {"labor": "labor_pct", "food": "food_cost_pct"}


def record_assignments(restaurants, members, partitions, groups, db_path=DB_PATH, today: date = None,
                       latest: dict = None) -> dict:
    """The peer assignment ledger (Benchmarking audit #29): per restaurant,
    week and metric family, the rung of the ladder it reached, the partition,
    a hash of the peer set (never the ids), n and organisations — the
    viewer's own organisation left out, as the band it is shown. A changed
    partition is logged as `comparison_group_changed` (read by
    confidence._recent_changes, #30), and a rung reached for the first time
    as `benchmark_rung_up` (#20; Home's "new comparison available" reads it).

    The rung is what the restaurant would be SHOWN (fix round #44, R2-19 /
    R4-30): "peers" only when a band on its ladder passes
    benchmarks.publish_row() for a metric it has measured — per-metric n
    after its organisation is out, the organisation cap, the floors, the
    spread gate and the age limit — at the finest rung that does (n and
    organisations are that band's); "platform" only when an all-restaurants
    band for a behaviour metric does; "published" only for a published
    figure measured the same way as Cavnar's (a figure defined differently
    is context, never a comparison). The stored bands are read once, not
    per restaurant."""
    import benchmark_registry as _br
    today = today or date.today()
    week = _features.iso_week(today)
    bands = {}
    conn = get_conn(db_path)
    try:
        for r in conn.execute(
                "SELECT b.cohort, b.metric, b.week, b.n, b.computed_at, b.members_json FROM intel_benchmarks b "
                "JOIN (SELECT cohort, metric, MAX(week) AS week FROM intel_benchmarks WHERE week >= ? "
                "GROUP BY cohort, metric) m ON m.cohort=b.cohort AND m.metric=b.metric AND m.week=b.week",
                (benchmarks._week_floor(today),)).fetchall():
            bands[(r["cohort"], r["metric"])] = dict(r)
    finally:
        conn.close()

    def shown(key, metrics, own_orgs, own_feats):
        """(n, orgs) of the best band a viewer is shown in `key`, or None."""
        best = None
        for metric in metrics:
            row = bands.get((key, metric))
            if row is None or (own_feats is not None and own_feats.get(metric) is None):
                continue
            p = benchmarks.publish_row(row, metric, exclude_org=own_orgs)
            if p and not p.get("withheld"):
                if best is None or p["n"] > best[0]:
                    best = (p["n"], p.get("orgs"))
        return best

    conn = get_conn(db_path)
    written = changed = up = 0
    try:
        prev = {}
        try:
            for r in conn.execute("SELECT restaurant_id, family, rung, partition_key FROM intel_peer_assignments "
                                  "WHERE week = (SELECT MAX(week) FROM intel_peer_assignments p2 WHERE "
                                  "p2.restaurant_id = intel_peer_assignments.restaurant_id AND p2.week < ?)",
                                  (week,)).fetchall():
                prev[(int(r["restaurant_id"]), r["family"])] = (r["rung"], r["partition_key"])
        except Exception:
            prev = {}
        for rest in restaurants:
            rid = rest.id
            m = (members or {}).get(rid) or {}
            parts = (partitions or {}).get(rid) or {}
            own_orgs = m.get("org_hashes") or ({m["org_hash"]} if m.get("org_hash") else {privacy.org_hash(f"r{rid}")})
            own_feats = ((latest or {}).get(rid) or {}).get("features") if latest is not None else None
            if latest is not None and own_feats is None:
                own_feats = {}
            for fam in categories.FAMILIES:
                key = parts.get(fam)
                rung, n, orgs, hsh, used = "self", None, None, None, None
                ladder = ((parts.get("_ladder") or {}).get(fam)) or ([key] if key else [])
                for k in ladder:
                    got = shown(k, _FAMILY_METRICS[fam], own_orgs, own_feats)
                    if got:
                        rung, (n, orgs), used = "peers", got, k
                        break
                if used:
                    stored = (groups or {}).get(fam, {}).get(used) or {}
                    others = sorted(r_ for r_, o in stored.get("members", []) if o not in own_orgs)
                    hsh = hashlib.sha256(json.dumps(others).encode()).hexdigest()[:16] if others else None
                if rung == "self" and fam == "format":
                    got = shown("platform", [mt for mt in _FAMILY_METRICS["format"] if _reg.platform_allowed(mt)],
                                own_orgs, own_feats)
                    if got:
                        rung, (n, orgs) = "platform", got
                if rung == "self" and fam in _FAMILY_INDUSTRY and (m.get("profile") or {}).get("confirmed"):
                    metric = _FAMILY_METRICS[fam][0]
                    if _br.lookup(_FAMILY_INDUSTRY[fam], (m.get("profile") or {}).get("concept"),
                                  definition=_reg.definition(metric),
                                  service_model=(m.get("profile") or {}).get("service_model")):
                        rung = "published"
                prof = m.get("profile") or {}
                conn.execute(
                    "INSERT INTO intel_peer_assignments (restaurant_id, week, family, rung, partition_key, "
                    "peer_set_hash, n, orgs, profile_source, profile_confirmed_at, drift) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(restaurant_id, week, family) DO UPDATE SET rung=excluded.rung, "
                    "partition_key=excluded.partition_key, peer_set_hash=excluded.peer_set_hash, n=excluded.n, "
                    "orgs=excluded.orgs, profile_source=excluded.profile_source, "
                    "profile_confirmed_at=excluded.profile_confirmed_at, drift=excluded.drift, "
                    "computed_at=datetime('now')",
                    (rid, week, fam, rung, key, hsh, n, orgs, prof.get("source"), prof.get("confirmed_at"),
                     parts.get("_drift") if fam == "labor" else None))
                written += 1
                before = prev.get((rid, fam))
                if before and before[1] != key:
                    conn.execute("INSERT INTO activity_log (restaurant_id, event_type, event_data) VALUES (?,?,?)",
                                 (rid, "comparison_group_changed",
                                  json.dumps({"family": fam, "from": before[1], "to": key, "week": week})))
                    changed += 1
                if before and _RUNG_ORDER.get(rung, 0) > _RUNG_ORDER.get(before[0], 0):
                    conn.execute("INSERT INTO activity_log (restaurant_id, event_type, event_data) VALUES (?,?,?)",
                                 (rid, "benchmark_rung_up",
                                  json.dumps({"family": fam, "from": before[0], "to": rung, "week": week,
                                              "group": used})))
                    up += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "groups_changed": changed, "rungs_up": up, "week": week}


def run_features(db_path=DB_PATH, today: date = None, wall_seconds=FEATURE_WALL_SECONDS, workers=FEATURE_WORKERS) -> dict:
    """The nightly per-restaurant feature pass (and Restaurant DNA beside
    it): bounded by wall_seconds, resumable from its cursor. Returns the
    standard counts (#39) — attempted, ok (computed), failed, skipped,
    hit_bound (the bound cut the pass short) — with its own keys."""
    today = today or date.today()
    rs = sorted(active_restaurants(db_path, include_demo=True), key=lambda r: r.id)
    if not rs:
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False,
                "computed": 0, "resumed_at": 0, "complete": True}
    start_after = _cursor_get(db_path)
    order = [r for r in rs if r.id > start_after] + [r for r in rs if r.id <= start_after]
    deadline = time.monotonic() + wall_seconds
    computed = failed = 0
    last_done = start_after
    stopped_early = False
    # Restaurant DNA (dna.py, Top-50 #24) is written beside the feature row
    # by the same bounded pass and cursor, from the features just computed.
    # The normalisation (stated anchors below MIN_ROBUST_N, robust z above)
    # is read once per pass.
    try:
        from . import dna as _dna
        norms = _dna.platform_norms(db_path=db_path)
    except Exception as e:
        print(f"[intelligence] DNA norms unavailable: {e}")
        _dna, norms = None, None
    # Each restaurant's own game medians (event_intel.peers, re-audit 10/1/26
    # P4-05) are written beside its feature row by the same bounded pass and
    # cursor: a request then reads one materialised row per member, never
    # another restaurant's nights. Who may teach is read once per pass.
    try:
        from event_intel import peers as _peers
        peer_real, peer_since = real_restaurant_ids(db_path=db_path), learning_since_by_id(db_path=db_path)
    except Exception as e:
        print(f"[intelligence] game peer medians unavailable: {e}")
        _peers, peer_real, peer_since = None, None, None

    def one(r):
        try:
            f = _features.compute_and_store(r.id, today=today, db_path=db_path)
        except Exception as e:      # one restaurant's failure never stops the pass
            return r.id, e
        if _dna is not None:
            try:
                _dna.compute_and_store(r.id, today=today, db_path=db_path, features=f, restaurant=r, norms=norms)
            except Exception as e:  # the profile failing never fails the feature row
                try:
                    import ops
                    ops.capture(e, job="intelligence_dna", context=f"restaurant_id={r.id}")
                except Exception:
                    pass
        if _peers is not None:
            try:
                _peers.store_member_effects(r.id, db_path=db_path, real=peer_real, since=peer_since)
            except Exception as e:  # nor do the game medians
                try:
                    import ops
                    ops.capture(e, job="event_peers", context=f"restaurant_id={r.id}")
                except Exception:
                    pass
        return r.id, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(order), workers):
            # The first batch always runs: a pass that never computes anything
            # would never move the cursor, and the same tail would starve.
            if i > 0 and time.monotonic() > deadline:
                stopped_early = True
                break
            batch = order[i:i + workers]
            for rid, err in pool.map(one, batch):
                if err is None:
                    computed += 1
                else:
                    failed += 1
                    try:
                        import ops
                        ops.capture(err, job="intelligence_features", context=f"restaurant_id={rid}")
                    except Exception:
                        pass
                last_done = rid
    # A completed sweep resets the cursor so the next night starts at the top.
    _cursor_set(db_path, last_done if stopped_early else 0)
    return {"attempted": computed + failed, "ok": computed, "failed": failed,
            "skipped": len(rs) - computed - failed, "hit_bound": stopped_early,
            "computed": computed, "resumed_at": start_after, "complete": not stopped_early,
            "week": _features.iso_week(today)}


# The features backfill (memory audit PLATFORM-5): past weeks computed from
# the raw tables, bounded by a wall clock and resumed from a cursor (the
# restaurant it stopped at) and a per-restaurant watermark (the last week it
# finished, under which FEATURES_VERSION) — run_daily_fetch's pattern.
BACKFILL_CURSOR = "intelligence_features_backfill"
BACKFILL_MARK = "intelligence_features_backfill:"
BACKFILL_WALL_SECONDS = 180


def _backfill_mark(db_path, rid):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (f"{BACKFILL_MARK}{int(rid)}",)).fetchone()
    finally:
        conn.close()
    raw = str(row["value"]) if row and row["value"] else ""
    ver, _, through = raw.partition("|")
    try:
        return int(ver), (through or None)
    except ValueError:
        return None, None


def _set_backfill_mark(db_path, rid, through):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                     (f"{BACKFILL_MARK}{int(rid)}", f"{_features.FEATURES_VERSION}|{through or ''}"))
        conn.commit()
    finally:
        conn.close()


def _backfill_floor(r):
    """A restaurant's earliest backfillable day beyond the raw data: a
    converted demo's seeded history is never read as its own past."""
    raw = getattr(r, "demo_cleared_at", None)
    if raw and not getattr(r, "is_demo", 0):
        try:
            return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def run_features_backfill(db_path=DB_PATH, today: date = None, wall_seconds=BACKFILL_WALL_SECONDS) -> dict:
    """Compute the past weeks each restaurant has raw data for and no row of
    the current FEATURES_VERSION (features.weeks_to_backfill: up to
    features.BACKFILL_MAX_WEEKS back, never this week — the nightly pass's),
    as of each week's last day, stored `backfilled`. Bounded by
    `wall_seconds` (the first week always runs, so the pass always moves),
    resumed from BACKFILL_CURSOR, and each restaurant's watermark moves week
    by week, so a stopped pass resumes mid-restaurant and a week the raw
    tables measure nothing in is not recomputed nightly. A FEATURES_VERSION
    bump starts every watermark over. Returns the standard counts."""
    today = today or date.today()
    rs = sorted(active_restaurants(db_path, include_demo=True), key=lambda r: r.id)
    if not rs:
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "weeks": 0}
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (BACKFILL_CURSOR,)).fetchone()
    finally:
        conn.close()
    try:
        start_after = int(row["value"]) if row and row["value"] else 0
    except (TypeError, ValueError):
        start_after = 0
    order = [r for r in rs if r.id > start_after] + [r for r in rs if r.id <= start_after]
    deadline = time.monotonic() + wall_seconds
    attempted = done = failed = weeks = 0
    stopped, last_done, worked = False, start_after, False
    last_week = _features.iso_week(today - timedelta(days=7))
    for r in order:
        if worked and time.monotonic() > deadline:
            stopped = True
            break
        attempted += 1
        ver, through = _backfill_mark(db_path, r.id)
        after = through if ver == _features.FEATURES_VERSION else None
        floor = _backfill_floor(r)
        if floor is not None:
            fw = _features.iso_week(floor - timedelta(days=7))
            after = max(after or "", fw) or None
        try:
            todo = _features.weeks_to_backfill(r.id, today=today, db_path=db_path, after_week=after)
        except Exception as e:
            failed += 1
            _capture(e, "intelligence_features_backfill", r.id)
            continue
        for wk in todo:
            if worked and time.monotonic() > deadline:
                stopped = True
                break
            worked = True
            try:
                weeks += 1 if _features.backfill_week(r.id, wk, db_path=db_path) else 0
            except Exception as e:          # one week's failure never stops the pass
                failed += 1
                _capture(e, "intelligence_features_backfill", r.id)
            _set_backfill_mark(db_path, r.id, wk)
        if stopped:
            break
        mark = max(last_week, todo[-1] if todo else last_week)
        if todo or ver != _features.FEATURES_VERSION or through != mark:
            _set_backfill_mark(db_path, r.id, mark)
        done += 1
        last_done = r.id
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                     (BACKFILL_CURSOR, str(int(last_done if stopped else 0))))
        conn.commit()
    finally:
        conn.close()
    return {"attempted": attempted, "ok": done, "failed": failed, "skipped": len(rs) - attempted,
            "hit_bound": stopped, "weeks": weeks, "resumed_at": start_after}


def _capture(e, job, rid):
    try:
        import ops
        ops.capture(e, job=job, context=f"restaurant_id={rid}")
    except Exception:
        pass


def features_sweep_complete(db_path=DB_PATH, today: date = None) -> bool:
    """Whether this ISO week's feature pass has reached every restaurant:
    its cursor is back at 0 and was last moved this week. A database whose
    feature pass has never run (no cursor row) is not held back."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT value, updated_at FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row:
        return True
    try:
        at = datetime.strptime(str(row["updated_at"])[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        at = None
    return str(row["value"] or "0") == "0" and at is not None and _features.iso_week(at) == _features.iso_week(today)


def _stage(out, name, fn):
    """Run one stage of the learning pass, guarded (R2-18): a failure is
    captured and recorded in the result, and the stages after it still
    run."""
    try:
        out[name] = fn()
        return out[name]
    except Exception as e:
        print(f"[intelligence] {name} failed: {e}")
        try:
            import ops
            ops.capture(e, job="intelligence_learning", context=name)
        except Exception:
            pass
        out[name] = {"error": str(e)}
        return None


def run_learning(db_path=DB_PATH, today: date = None) -> dict:
    """The nightly learning pass. Each stage is guarded (one failure no
    longer loses the night's bands), and this week's bands are only written
    — and so frozen for the week — once the feature pass has reached every
    restaurant (fix round #40, R2-18 / R1-21): a band frozen off a partial
    sweep stood all week without the restaurants the sweep had not reached."""
    today = today or date.today()
    rs = active_restaurants(db_path)
    cohorts = cohorts_for(rs)
    members = member_info(db_path=db_path, today=today)
    # Once: pooled rows built before Google user data was kept out of them
    # (intelligence.provenance) are dropped and rebuilt tonight.
    _stage({}, "google_purge", lambda: purge_google_pooled(db_path=db_path))
    latest = _features.latest_by_restaurant(db_path=db_path)
    partitions = peer_partitions(members, latest=latest, db_path=db_path, today=today)
    out = {"restaurants": len(rs), "cohorts": len({c for c in cohorts.values() if c})}
    # Every restaurant's labels, seeded and excluded ones too (PLATFORM-15):
    # a row's cohort, partition and Google flag are facts about its
    # restaurant; who may teach is each reader's rule.
    _stage(out, "feedback", lambda: feedback.sync(db_path=db_path, labels=learning_labels(db_path=db_path)))
    _stage(out, "patterns", lambda: patterns.discover(db_path=db_path, cohorts=cohorts, today=today, members=members))
    groups = {}
    if features_sweep_complete(db_path=db_path, today=today):
        bm = _stage(out, "benchmarks", lambda: benchmarks.compute(db_path=db_path, cohorts=partitions, today=today,
                                                                  members=members))
        if isinstance(bm, dict):
            groups = bm.pop("groups", {}) or {}
    else:
        out["benchmarks"] = {"written": 0, "held": "this week's feature pass has not reached every restaurant yet — "
                                                   "the bands are written once it has"}
    # The ledger asks what each restaurant measured on its OWN side of a
    # comparison (it is shown its own rating beside a band whether or not
    # its reviews come through Google), so it reads the un-pooled rows.
    own_latest = _features.latest_by_restaurant(db_path=db_path, pooled=False)
    _stage(out, "peer_ledger", lambda: record_assignments(rs, members, partitions, groups, db_path=db_path,
                                                          today=today, latest=own_latest))
    from . import trends
    _stage(out, "cohort_series", lambda: trends.persist(cohorts=partitions, members=members, db_path=db_path,
                                                        today=today))
    _stage(out, "confidence_log", lambda: log_confidence(db_path=db_path, cohorts=cohorts, today=today))
    # Member lists leave bands no reader serves any more (FORGET-8).
    _stage(out, "band_members", lambda: benchmarks.strip_old_members(db_path=db_path, today=today))
    # The engine's comparisons, materialised per restaurant after tonight's
    # bands (BM4-14, Top-50 #46): bounded and resumable like every pass here.
    try:
        from . import comparison_cache
        out["benchmark_facts"] = comparison_cache.materialise(db_path=db_path, today=today)
    except Exception as e:
        print(f"[intelligence] benchmark facts not materialised: {e}")
        out["benchmark_facts"] = {"error": str(e)}
    # Neighbour predictions (predict.py): weekly, bounded, resumable — and
    # unavailable below the floors, which is every restaurant today.
    try:
        from . import predict
        out["effects"] = predict.run_weekly(db_path=db_path, today=today)
    except Exception as e:
        print(f"[intelligence] effects not computed: {e}")
        out["effects"] = {"error": str(e)}
    # The standard counts (#39), by stage: a stage that raised is failed
    # (and captured), the bands held for an unfinished feature pass skipped.
    stages = [k for k in ("feedback", "patterns", "benchmarks", "peer_ledger", "cohort_series", "confidence_log",
                          "band_members", "benchmark_facts", "effects") if k in out]
    failed = sum(1 for k in stages if isinstance(out[k], dict) and out[k].get("error"))
    held = 1 if isinstance(out.get("benchmarks"), dict) and out["benchmarks"].get("held") else 0
    out.update(attempted=len(stages) - held, ok=len(stages) - held - failed, failed=failed, skipped=held,
               hit_bound=False)
    return out


# The confidence log reads the learning table over this window only
# (BM4-14): it selected every intel_rec_events row ever written, every night.
CONFIDENCE_LOG_WINDOW_DAYS = 365


def log_confidence(db_path=DB_PATH, cohorts: dict = None, today: date = None) -> dict:
    """The week's acceptance and success by kind, per cohort and platform-
    wide, so the dashboard can draw model confidence over time — over the
    last CONFIDENCE_LOG_WINDOW_DAYS of events (memory audit PLATFORM-14):

      * mean_confidence averages only snapshots of the CURRENT trust version
        (confidence_engine.VERSION, stored as `trust_version`): a version-1
        % measured something else, and mixing them drew two meanings on one
        line;
      * `n` counts recommendations (episodes), not event rows;
      * the floors count organisations (privacy.org_map, via scoring), and
        `orgs` is stored beside n;
      * the rows of restaurants that may not teach (excluded_learning_ids)
        and Google user data (provenance) are never in it.

    The row is the ISO WEEK's own (memory re-audit 9/29/26, PLATFORM-15):
    n, mean_confidence, acceptance_rate, success_rate and orgs over the
    events stamped in that week — each rate NULL below its own floor — with
    the trailing-year figures in `trailing_*`. The weekly row was the
    trailing year's, so "confidence over time" was a 52-week moving average
    and a trust-version change took months to show. A row is written when
    the trailing year clears the floor."""
    from datetime import timedelta
    try:
        import confidence_engine as _ce
        current_tv = int(_ce.VERSION)
    except Exception:
        current_tv = None
    today = today or date.today()
    week = _features.iso_week(today)
    since = (today - timedelta(days=CONFIDENCE_LOG_WINDOW_DAYS)).isoformat()
    cohorts = cohorts or {}
    written = 0
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, source_key, rec_kind, action, outcome, confidence_at, trust_version, "
                            "days_to_effect, event_at FROM intel_rec_events WHERE event_at >= ? "
                            "AND COALESCE(google_data, 0) = 0", (since,)).fetchall()
    finally:
        conn.close()
    excluded = excluded_learning_ids(db_path)     # demo, test, internal (CA3 F7, memory audit)
    learning_since = learning_since_by_id(db_path)   # a converted demo's demo era (INT #20)
    rows = [r for r in rows if r["restaurant_id"] not in excluded
            and not before_learning(r["restaurant_id"], r["event_at"], learning_since)]
    orgs = scoring.org_map([r["restaurant_id"] for r in rows], db_path=db_path)
    groups = {}
    for r in rows:
        groups.setdefault(("platform", r["rec_kind"]), []).append(r)
        c = cohorts.get(r["restaurant_id"])
        if c:
            groups.setdefault((c, r["rec_kind"]), []).append(r)
    monday = today - timedelta(days=today.weekday())
    week_lo, week_hi = monday.isoformat(), (monday + timedelta(days=7)).isoformat()

    def figures(rs_):
        s = scoring._summarise(rs_, orgs=orgs)
        confs = [float(r["confidence_at"]) for r in rs_ if r["confidence_at"] is not None
                 and (current_tv is None or r["trust_version"] == current_tv)]
        recs = {scoring._rec(r) for r in rs_ if r["action"] != "measured"}
        n_orgs = len({orgs.get(r["restaurant_id"], f"r{r['restaurant_id']}") for r in rs_})
        return {"n": len(recs), "orgs": n_orgs, "available": s["available"],
                "mean": round(sum(confs) / len(confs), 3) if (confs and s["available"]) else None,
                "acc": s["acceptance_rate"] if s.get("acceptance_available") else None,
                "suc": s["success_rate"] if s.get("success_available") else None}

    conn = get_conn(db_path)
    try:
        for (cohort, kind), rs_ in groups.items():
            year = figures(rs_)
            if not year["available"]:
                continue
            wk = figures([r for r in rs_ if week_lo <= str(r["event_at"] or "").replace("T", " ") < week_hi])
            conn.execute("INSERT INTO intel_confidence_log (week, cohort, rec_kind, n, mean_confidence, acceptance_rate, "
                         "success_rate, trust_version, orgs, trailing_n, trailing_mean_confidence, "
                         "trailing_acceptance_rate, trailing_success_rate, trailing_orgs) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(week, cohort, rec_kind) DO UPDATE SET n=excluded.n, "
                         "mean_confidence=excluded.mean_confidence, acceptance_rate=excluded.acceptance_rate, "
                         "success_rate=excluded.success_rate, trust_version=excluded.trust_version, "
                         "orgs=excluded.orgs, trailing_n=excluded.trailing_n, "
                         "trailing_mean_confidence=excluded.trailing_mean_confidence, "
                         "trailing_acceptance_rate=excluded.trailing_acceptance_rate, "
                         "trailing_success_rate=excluded.trailing_success_rate, trailing_orgs=excluded.trailing_orgs, "
                         "computed_at=datetime('now')",
                         (week, cohort, kind, wk["n"], wk["mean"], wk["acc"], wk["suc"], current_tv, wk["orgs"],
                          year["n"], year["mean"], year["acc"], year["suc"], year["orgs"]))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "week": week}


# Pooled rows computed before the Google rule (intelligence.provenance):
# review metrics' bands and cohort series and the review hypotheses'
# patterns, dropped once and rebuilt that night from the Google-free view.
GOOGLE_PURGE_MARK = "intelligence_google_purge:v1"


def purge_google_pooled(db_path=DB_PATH) -> dict:
    """Once (GOOGLE_PURGE_MARK): drop the pooled rows a Google-connected
    restaurant's review figures may have entered before the rule — every
    intel_benchmarks and intel_cohort_series row of a review metric, the
    materialised comparisons and neighbour predictions built on them
    (intel_benchmark_facts, intel_effects), and every intel_patterns row
    whose hypothesis reads a review feature — so the night's pass rebuilds
    them from features.cross_restaurant_view (the bands' weekly freeze
    starts again with the first rebuilt one). Derived, recomputable
    aggregates only; nothing a restaurant entered. Returns {"purged": n}
    (0 once done)."""
    from . import provenance
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (GOOGLE_PURGE_MARK,)).fetchone():
            return {"purged": 0}
        metrics = [m for m in _features.BENCHMARK_KEYS if provenance.review_metric(m)]
        marks = ",".join("?" for _ in metrics)
        n = 0
        for table in ("intel_benchmarks", "intel_cohort_series", "intel_benchmark_facts"):
            n += conn.execute(f"DELETE FROM {table} WHERE metric IN ({marks})", metrics).rowcount or 0
        # predictions keyed by an outcome metric (metrics.py's review metrics)
        n += conn.execute("DELETE FROM intel_effects WHERE metric IN ('avg_rating','response_hours') "
                          "OR metric LIKE 'complaints%'").rowcount or 0
        hyps = [h["key"] for h in patterns.HYPOTHESES + patterns.PROSPECTIVE_HYPOTHESES
                if provenance.review_metric(h["outcome"]) or provenance.review_metric(h["behaviour"][0])]
        if hyps:
            n += conn.execute(f"DELETE FROM intel_patterns WHERE hypothesis IN ({','.join('?' for _ in hyps)})",
                              hyps).rowcount or 0
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?, '1', datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value='1', updated_at=datetime('now')", (GOOGLE_PURGE_MARK,))
        conn.commit()
    finally:
        conn.close()
    return {"purged": n}
