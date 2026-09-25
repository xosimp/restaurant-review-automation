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


def member_info(db_path=DB_PATH, today: date = None) -> dict:
    """{restaurant_id: {org, org_hash, place_id, excluded, live_weeks,
    cost_basis, profile, category, type_source}} for every restaurant row —
    what the band builders need to count organisations, merge duplicate
    listings and decide eligibility. Server-side only; never a payload."""
    import thresholds as _thr
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM restaurants").fetchall()
    finally:
        conn.close()
    out = {}
    for row in rows:
        d = dict(row)
        ns = SimpleNamespace(**d)
        cat, src = categories.category_for(ns)
        org = privacy.org_key(d)
        out[int(d["id"])] = {
            "org": org, "org_hash": privacy.org_hash(org),
            "place_id": (str(d.get("google_place_id") or "").strip() or None),
            "excluded": bool(d.get("exclude_from_learning")),
            "live_weeks": _weeks_since(d.get("created_at"), today),
            "cost_basis": _thr.labor_cost_basis(d),
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
    if (member.get("live_weeks") or 0) < MIN_LIVE_WEEKS:
        return f"fewer than {MIN_LIVE_WEEKS} weeks live on Cavnar"
    try:
        comp = float((row or {}).get("completeness"))
    except (TypeError, ValueError):
        comp = None
    if comp is not None and comp < MIN_MEMBER_COMPLETENESS:
        return "less than half of its measures on file"
    return None


# ── the peer partition, with hysteresis (Benchmarking audit #20, #30) ─────
# The partition is the owner-CONFIRMED profile. A measured signal that
# disagrees with it (alcohol share against bar-led) moves it only after
# DRIFT_WEEKS consecutive weeks of disagreement — never on one week's
# figures, and never on a text edit.
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


def _previous_drift(db_path, today) -> dict:
    """{restaurant_id: [drift of each earlier week, newest first]}."""
    week = _features.iso_week(today)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, week, drift FROM intel_peer_assignments WHERE family='labor' "
                            "AND week < ? ORDER BY week DESC", (week,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(int(r["restaurant_id"]), []).append(r["drift"])
    return out


def peer_partitions(members, latest=None, db_path=DB_PATH, today: date = None) -> dict:
    """{restaurant_id: {family: partition key or None, "_drift": str|None}}
    for every member: the hard split each metric family is read from
    (categories.partition_key), built from the confirmed profile only."""
    today = today or date.today()
    latest = latest if latest is not None else _features.latest_by_restaurant(db_path=db_path)
    prev = _previous_drift(db_path, today)
    out = {}
    for rid, m in (members or {}).items():
        prof = dict(m.get("profile") or {})
        feats = ((latest or {}).get(rid) or {}).get("features") or {}
        drift = _measured_drift(prof, feats)
        history = prev.get(rid, [])[:DRIFT_WEEKS - 1]
        if drift and len(history) == DRIFT_WEEKS - 1 and all(h == drift for h in history):
            prof["bar_led"] = drift == "bar_led"      # four measured weeks moved it
        vb = feats.get("volume_band")
        out[rid] = {fam: categories.partition_key(prof, fam) for fam in categories.FAMILIES}
        out[rid]["staff"] = categories.partition_key(prof, "staff", volume_band=vb)
        out[rid]["_drift"] = drift
    return out


_RUNG_ORDER = {"self": 0, "published": 1, "platform": 2, "peers": 3}
_FAMILY_METRICS = {"format": [m for m in _features.BENCHMARK_KEYS if _reg.partition_family(m) == "format"],
                   "labor": [m for m in _features.BENCHMARK_KEYS if _reg.partition_family(m) == "labor"],
                   "food": [m for m in _features.BENCHMARK_KEYS if _reg.partition_family(m) == "food"]}
_FAMILY_INDUSTRY = {"labor": "labor_pct", "food": "food_cost_pct"}


def record_assignments(restaurants, members, partitions, groups, db_path=DB_PATH, today: date = None) -> dict:
    """The peer assignment ledger (Benchmarking audit #29): per restaurant,
    week and metric family, the rung of the ladder it reached, the partition,
    a hash of the peer set (never the ids), n and organisations — the
    viewer's own organisation left out, as the band it is shown. A changed
    partition is logged as `comparison_group_changed` (read by
    confidence._recent_changes, #30), and a rung reached for the first time
    as `benchmark_rung_up` (#20). In memory from compute()'s groups: one
    write per restaurant and family, no query per restaurant."""
    import benchmark_registry as _br
    today = today or date.today()
    week = _features.iso_week(today)
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
            own_org = m.get("org_hash")
            for fam in categories.FAMILIES:
                key = parts.get(fam)
                rung, n, orgs, hsh = "self", None, None, None
                if key:
                    stored = (groups or {}).get(fam, {}).get(key) or {}
                    others = [(r_, o) for r_, o in stored.get("members", []) if o != own_org]
                    n = len(others)
                    orgs = len({o for _r, o in others})
                    hsh = hashlib.sha256(json.dumps(sorted(r_ for r_, _o in others)).encode()).hexdigest()[:16]                         if others else None
                    if n >= benchmarks.MIN_QUARTILE_N and orgs >= privacy.MIN_ORGS and stored.get("metrics"):
                        rung = "peers"
                if rung == "self" and fam == "format":
                    plat = (groups or {}).get("format", {}).get("platform") or {}
                    others = [o for _r, o in plat.get("members", []) if o != own_org]
                    if len(others) >= benchmarks.MIN_QUARTILE_N and len(set(others)) >= privacy.MIN_ORGS                             and plat.get("metrics"):
                        rung = "platform"
                if rung == "self" and fam in _FAMILY_INDUSTRY and (m.get("profile") or {}).get("confirmed"):
                    if _br.lookup(_FAMILY_INDUSTRY[fam], (m.get("profile") or {}).get("concept"),
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
                                  json.dumps({"family": fam, "from": before[0], "to": rung, "week": week})))
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


def run_learning(db_path=DB_PATH, today: date = None) -> dict:
    today = today or date.today()
    rs = active_restaurants(db_path)
    cohorts = cohorts_for(rs)
    members = member_info(db_path=db_path, today=today)
    latest = _features.latest_by_restaurant(db_path=db_path)
    partitions = peer_partitions(members, latest=latest, db_path=db_path, today=today)
    out = {"restaurants": len(rs), "cohorts": len({c for c in cohorts.values() if c})}
    out["feedback"] = feedback.sync(db_path=db_path, cohorts=cohorts)
    out["patterns"] = patterns.discover(db_path=db_path, cohorts=cohorts, today=today, members=members)
    bm = benchmarks.compute(db_path=db_path, cohorts=partitions, today=today, members=members)
    groups = bm.pop("groups", {})
    out["benchmarks"] = bm
    out["peer_ledger"] = record_assignments(rs, members, partitions, groups, db_path=db_path, today=today)
    from . import trends
    out["cohort_series"] = trends.persist(cohorts=partitions, members=members, db_path=db_path, today=today)
    out["confidence_log"] = log_confidence(db_path=db_path, cohorts=cohorts, today=today)
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
