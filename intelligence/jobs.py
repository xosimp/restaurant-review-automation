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
from datetime import date, datetime
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


def seeded_restaurant_ids(db_path=DB_PATH) -> set:
    """The complement of real_restaurant_ids within the restaurants table:
    demo accounts and recently de-flagged ones. Readers of the feature and
    event tables drop these ids."""
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(SEEDED_RESTAURANT_SQL, (f"-{SEEDED_HISTORY_DAYS} days",)).fetchall()
        except Exception:
            rows = conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=1").fetchall()
    finally:
        conn.close()
    return {int(r["id"]) for r in rows}


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
        try:
            rows = conn.execute(REAL_RESTAURANT_SQL, (f"-{SEEDED_HISTORY_DAYS} days",)).fetchall()
        except Exception:
            rows = conn.execute("SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=0").fetchall()
    finally:
        conn.close()
    return {int(r["id"]) for r in rows}


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
            "excluded": bool(d.get("exclude_from_learning")),
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
        return f"fewer than {MIN_LIVE_WEEKS} weeks live on Cavnar"
    try:
        comp = float((row or {}).get("completeness"))
    except (TypeError, ValueError):
        comp = None
    if comp is not None and comp < MIN_MEMBER_COMPLETENESS:
        return "less than half of its measures on file"
    return None


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
    today = today or date.today()
    rs = sorted(active_restaurants(db_path, include_demo=True), key=lambda r: r.id)
    if not rs:
        return {"computed": 0, "skipped": 0, "resumed_at": 0, "complete": True}
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
    return {"computed": computed, "failed": failed, "resumed_at": start_after, "complete": not stopped_early,
            "week": _features.iso_week(today)}


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
    latest = _features.latest_by_restaurant(db_path=db_path)
    partitions = peer_partitions(members, latest=latest, db_path=db_path, today=today)
    out = {"restaurants": len(rs), "cohorts": len({c for c in cohorts.values() if c})}
    _stage(out, "feedback", lambda: feedback.sync(db_path=db_path, cohorts=cohorts))
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
    _stage(out, "peer_ledger", lambda: record_assignments(rs, members, partitions, groups, db_path=db_path,
                                                          today=today, latest=latest))
    from . import trends
    _stage(out, "cohort_series", lambda: trends.persist(cohorts=partitions, members=members, db_path=db_path,
                                                        today=today))
    _stage(out, "confidence_log", lambda: log_confidence(db_path=db_path, cohorts=cohorts, today=today))
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
    return out


# The confidence log reads the learning table over this window only
# (BM4-14): it selected every intel_rec_events row ever written, every night.
CONFIDENCE_LOG_WINDOW_DAYS = 365


def log_confidence(db_path=DB_PATH, cohorts: dict = None, today: date = None) -> dict:
    """The week's acceptance and success by kind, per cohort and platform-
    wide, so the dashboard can draw model confidence over time — over the
    last CONFIDENCE_LOG_WINDOW_DAYS of events."""
    from datetime import timedelta
    today = today or date.today()
    week = _features.iso_week(today)
    since = (today - timedelta(days=CONFIDENCE_LOG_WINDOW_DAYS)).isoformat()
    cohorts = cohorts or {}
    written = 0
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, source_key, rec_kind, action, outcome, confidence_at, days_to_effect "
                            "FROM intel_rec_events WHERE event_at >= ?", (since,)).fetchall()
    finally:
        conn.close()
    groups = {}
    seeded = seeded_restaurant_ids(db_path)      # demo accounts never in a platform rate (CA3 F7)
    for r in rows:
        if r["restaurant_id"] in seeded:
            continue
        groups.setdefault(("platform", r["rec_kind"]), []).append(r)
        c = cohorts.get(r["restaurant_id"])
        if c:
            groups.setdefault((c, r["rec_kind"]), []).append(r)
    conn = get_conn(db_path)
    try:
        for (cohort, kind), rs_ in groups.items():
            s = scoring._summarise(rs_)
            if not s["available"]:
                continue
            confs = [float(r["confidence_at"]) for r in rs_ if r["confidence_at"] is not None]
            conn.execute("INSERT INTO intel_confidence_log (week, cohort, rec_kind, n, mean_confidence, acceptance_rate, success_rate) "
                         "VALUES (?,?,?,?,?,?,?) ON CONFLICT(week, cohort, rec_kind) DO UPDATE SET n=excluded.n, "
                         "mean_confidence=excluded.mean_confidence, acceptance_rate=excluded.acceptance_rate, "
                         "success_rate=excluded.success_rate, computed_at=datetime('now')",
                         (week, cohort, kind, len(rs_), round(sum(confs) / len(confs), 3) if confs else None,
                          s["acceptance_rate"], s["success_rate"]))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "week": week}
