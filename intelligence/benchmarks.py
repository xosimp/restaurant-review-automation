"""Level 3: benchmarks — where a restaurant sits among restaurants like it.

Computed weekly per cohort per metric from the latest feature row of each
restaurant, stored when the cohort clears the floor, and read back as a
band (p25/p50/p75) plus this restaurant's own standing. The restaurant's
own value is shown only to its own owner; the band is what crosses the
tenant line.

What crosses it is held to four rules (Never-Say audit NS4 H4/H5/M5/M6,
NS6 §B findings 1 and 5):

* Quartiles only over MIN_QUARTILE_N other restaurants, the VIEWER'S OWN
  ROW TAKEN OUT (`published(exclude_value=…)`), and rounded to a coarse
  step per metric. Linear-interpolated quartiles of five values sit exactly
  on the 2nd, 3rd and 4th members, so an owner who knows their own figure
  read three peers' exact labor % off the band. Fewer than MIN_QUARTILE_N →
  the band is withheld, never published.
* A band older than MAX_BAND_AGE_WEEKS is not served, and neither is the
  restaurant's own value past MAX_OWN_AGE_WEEKS; every band carries its
  `as_of` (M/D/YY) and every prompt line says it.
* The cohort label comes from the cohort ACTUALLY used: a platform band is
  "All restaurants on Cavnar", never "restaurants like yours".
* A cohort picked from a type Cavnar inferred from the restaurant's name
  carries `inferred`, and the line says so.

Benchmarking audit 9/24/26 (#6, #8, #9, #20, #39, #42):

* The cohort is the owner-CONFIRMED peer partition (categories.
  partition_key: service model, × bar-led for labor and food cost, × menu
  family for food cost and waste). A type Cavnar guessed is never a member
  and never counts toward a floor; `other` and untyped groups are never
  published.
* Only a behaviour metric (metrics_registry.platform_allowed) may fall back
  to the all-restaurants band. Labor %, hours and staff per $1k, food cost,
  waste and rating never do: the answer is "no like-for-like peers yet".
* Floors count ORGANISATIONS: at least MIN_QUARTILE_N others from at least
  privacy.MIN_ORGS organisations, the viewer's WHOLE organisation taken out
  (each member value is stored beside an org hash, server-side), duplicate
  Google listings merged. Fix round: an organisation is its org_map
  component (organisation, owner email, shared owner logins, Stripe
  customer — #13); one over a third is held to a third rather than
  withholding the band (#35); a band is only ever shown to a viewer, and
  never an all-types band for a type-dependent measure (#41).
* Members are eligible only when live MIN_LIVE_WEEKS, at least half their
  measures on file, not excluded from learning, a live customer (#40), and
  — for labor-cost metrics — with most of their hours on owner-set rates
  (labor_sourced).
* The quality gate withholds a band too spread to mean "alike", and any
  group with `other` or `mixed` in it (#22).
* Each member stands in every rung of its ladder (categories.
  partition_ladder, #34/#36); a viewer reads the finest rung that clears.
* What is published is frozen for the ISO week (the first computation of a
  week stands), quartiles come from the Harrell–Davis estimator (never one
  member's exact figure), and the rating step is 0.25★.
"""
import json
import math
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy, categories
from . import features as _features
from . import metrics_registry as _reg
from .stats import percentile, mean, harrell_davis


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# Other restaurants a published quartile band needs (the viewer excluded).
# At 8+ the interpolated quartiles fall between members for most n, and the
# coarse rounding below covers the n where they do not.
MIN_QUARTILE_N = 8
# A band, or this restaurant's own feature row, older than this is history,
# not a comparison (NS4 H5: a 2025-W39 band was served on 9/24/26).
MAX_BAND_AGE_WEEKS = 8
MAX_OWN_AGE_WEEKS = 8

PLATFORM_LABEL = "All restaurants on Cavnar AI"

# The published precision per metric: coarse enough that a quartile cannot
# be read back as one member's exact figure.
_STEP = {"avg_rating_30d": 0.25, "labor_pct_28d": 0.5, "labor_pct_sd_28d": 0.5, "food_cost_pct_28d": 0.5,
         "waste_sales_pct_28d": 0.5, "post_lift_median_28d": 0.5, "labor_hours_per_1k_28d": 0.1,
         "labor_hours_per_1k_day_28d": 0.1, "labor_hours_per_1k_night_28d": 0.1,
         "response_24h_rate_30d": 0.05, "reply_rate_30d": 0.05, "campaign_tap_rate_28d": 0.01,
         "post_engagement_rate_28d": 0.01, "outcomes_improved_rate_90d": 0.05}


BETTER = {"avg_rating_30d": "higher", "response_24h_rate_30d": "higher", "reply_rate_30d": "higher",
          "labor_pct_28d": "lower", "labor_pct_sd_28d": "lower", "food_cost_pct_28d": "lower",
          "labor_hours_per_1k_28d": "lower", "labor_hours_per_1k_day_28d": "lower", "labor_hours_per_1k_night_28d": "lower",
          "waste_sales_pct_28d": "lower", "campaign_tap_rate_28d": "higher", "outcomes_improved_rate_90d": "higher",
          "post_lift_median_28d": "higher", "post_engagement_rate_28d": "higher"}

LABELS = {"avg_rating_30d": "Average rating (30d)", "response_24h_rate_30d": "Reviews answered within a day",
          "reply_rate_30d": "Reviews answered", "labor_pct_28d": "Labor %", "labor_pct_sd_28d": "Day-to-day labor swing",
          "labor_hours_per_1k_28d": "Labor hours per $1k of sales", "labor_hours_per_1k_day_28d": "Labor hours per $1k, lunch/day",
          "labor_hours_per_1k_night_28d": "Labor hours per $1k, dinner/night",
          "food_cost_pct_28d": "Food cost %", "waste_sales_pct_28d": "Waste as % of sales",
          "campaign_tap_rate_28d": "Text campaign tap rate", "outcomes_improved_rate_90d": "Recommendations that measurably improved",
          "post_lift_median_28d": "Sales lift after a post (median)", "post_engagement_rate_28d": "Post engagement rate"}


def cohort_label(cohort) -> str:
    """The label of the cohort a band was ACTUALLY read from (NS4 H4): a
    peer partition in words ("Full-service, bar-led restaurants on
    Cavnar"), a type, or every restaurant on Cavnar."""
    if not cohort or cohort == "platform":
        return PLATFORM_LABEL
    if categories.is_partition(cohort):
        return categories.partition_label(cohort)
    return f"{categories.label(cohort)} on Cavnar AI"


def publishable_group(cohort) -> bool:
    """Never publish "other" or an untyped group (#39, BM1-14) — nor a
    partition carrying either as a coordinate: `sm:full_service|mixed` was
    the catch-all food group every unmapped concept and `other` fell into
    (fix round #22, R1-06/R2-6)."""
    c = str(cohort or "").strip().lower()
    if not c or c in ("other", "none", "uncategorised", "sm:other"):
        return False
    if categories.is_partition(c):
        segs = c[3:].split("|")
        if any(s in ("other", "mixed", "none", "") for s in segs):
            return False
    return True


def coarse(metric, x):
    """A published statistic at its metric's step (2 significant figures
    for a metric with none)."""
    if x is None:
        return None
    step = _STEP.get(metric)
    if step is None and str(metric).startswith("staff_per_1k."):
        step = 0.05
    x = float(x)
    if step is None:
        if x == 0:
            return 0.0
        mag = 10 ** (math.floor(math.log10(abs(x))) - 1)
        return round(round(x / mag) * mag, 6)
    return round(round(x / step) * step, 6)


def _week_monday(week: str):
    try:
        y, w = str(week).split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except Exception:
        return None


def _as_of(row) -> str | None:
    """M/D/YY the band was computed (else the end of its ISO week)."""
    from time_utils import mdy
    raw = (row or {}).get("computed_at")
    if raw:
        try:
            return mdy(datetime.strptime(str(raw)[:10], "%Y-%m-%d").date())
        except Exception:
            pass
    mon = _week_monday((row or {}).get("week"))
    return mdy(mon + timedelta(days=6)) if mon else None


def _week_floor(today: date = None, weeks: int = MAX_BAND_AGE_WEEKS) -> str:
    return _features.iso_week((today or date.today()) - timedelta(weeks=weeks))


def _key_for(entry, family):
    """A cohort map entry is a key for every metric (a string — tests and
    older callers) or a {family: key} partition map (jobs.peer_partitions)."""
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get(family)
    return entry


def labor_sourced(member) -> bool:
    """Whether a member's labor cost may stand in a labor-cost band: its
    owner-set rates cover LABOR_SOURCED_SHARE of its hours
    (jobs.member_info `labor_cost_sourced`); a member described only by its
    cost basis (older callers) is out on the $26 default."""
    m = member or {}
    if "labor_cost_sourced" in m:
        return bool(m["labor_cost_sourced"])
    return m.get("cost_basis") != "default"


def _keys_for(entry, family) -> list:
    """Every group a member stands in for one family: its ladder
    (jobs.peer_partitions `_ladder`, finest → coarsest — each rung is its
    own band) or the single key."""
    if isinstance(entry, dict) and isinstance(entry.get("_ladder"), dict):
        rungs = entry["_ladder"].get(family)
        if rungs:
            return list(rungs)
    key = _key_for(entry, family)
    return [key] if key else []


def compute(db_path=DB_PATH, cohorts: dict = None, today: date = None, members: dict = None) -> dict:
    """Store this ISO week's band for every (cohort, metric) that clears the
    floors. `cohorts` is {restaurant_id: key or {family: key}}; `members` is
    jobs.member_info (read here when not given). Returns {written, week,
    withheld, groups} — groups is the in-memory membership the peer ledger
    reads (org hashes, never a payload)."""
    latest = _features.latest_by_restaurant(db_path=db_path)
    today = today or date.today()
    week = _features.iso_week(today)
    if members is None:
        from .jobs import member_info
        members = member_info(db_path=db_path, today=today)
    if cohorts is None:
        # Each restaurant's confirmed peer partitions (jobs.peer_partitions).
        from .jobs import peer_partitions
        cohorts = peer_partitions(members, latest=latest, db_path=db_path, today=today)
    cohorts = cohorts or {}
    # Eligible members only, one per Google listing (#9, #39).
    from .jobs import ineligible
    elig, seen_place, skipped = {}, {}, {}
    for rid in sorted(latest):
        m = members.get(rid) or {"org": f"r{rid}", "org_hash": privacy.org_hash(f"r{rid}")}
        why = ineligible(m, latest[rid]) if members.get(rid) else None
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

    def typed(rid):
        # A string cohort map (older callers) is trusted only for a type the
        # owner or admin SET; a partition map is confirmed by construction.
        m = members.get(rid)
        return m is None or m.get("type_source") == "set" or (m.get("profile") or {}).get("confirmed")

    families = ("format", "labor", "food", "staff")
    groups = {fam: {} for fam in families}
    for fam in families:
        groups[fam]["platform"] = [rid for rid in elig]
    for rid in elig:
        entry = cohorts.get(rid)
        for fam in families:
            for key in _keys_for(entry, fam):
                if not key or key == "platform" or not publishable_group(key):
                    continue
                if not isinstance(entry, dict) and not typed(rid):
                    continue
                if rid not in groups[fam].setdefault(key, []):
                    groups[fam][key].append(rid)

    written = withheld = 0
    info = {fam: {} for fam in families}
    conn = get_conn(db_path)
    try:
        existing = {(r["cohort"], r["metric"]) for r in conn.execute(
            "SELECT cohort, metric FROM intel_benchmarks WHERE week=? AND members_json IS NOT NULL",
            (week,)).fetchall()}
        staffing = sorted({k for rid in elig for k in (latest[rid]["features"] or {}) if k.startswith("staff_per_1k.")})
        for metric in tuple(_features.BENCHMARK_KEYS) + tuple(staffing):
            fam = _reg.partition_family(metric)
            for cohort, rids in groups[fam].items():
                info[fam].setdefault(cohort, {"members": [(r, elig[r]["org_hash"]) for r in rids], "metrics": []})
                # Only a behaviour metric has an all-restaurants band (#6).
                if cohort == "platform" and not _reg.platform_allowed(metric):
                    continue
                pairs = []
                for r in rids:
                    v = (latest[r]["features"] or {}).get(metric)
                    if v is None:
                        continue
                    if metric in _reg.LABOR_COST_METRICS and not labor_sourced(elig[r]):
                        continue          # an assumed wage is not a labor cost (#14, fix round #11)
                    pairs.append((round(float(v), 3), elig[r]["org_hash"]))
                orgs, share = privacy.org_counts([o for _v, o in pairs])
                if not privacy.cohort_ok(len(pairs)) or orgs < privacy.MIN_ORGS:
                    continue
                info[fam][cohort]["metrics"].append(metric)
                if (cohort, metric) in existing:
                    continue      # frozen for the week: the first computation stands (#42)
                vals = [v for v, _o in pairs]
                # The member values, each beside its organisation's hash, stay
                # server-side (never selected into a payload): published()
                # takes the viewer's whole organisation out of what it shows.
                pairs.sort()
                conn.execute(
                    "INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, vals_json, orgs, "
                    "max_org_share, members_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(cohort, metric, week) DO UPDATE SET n=excluded.n, p25=excluded.p25, "
                    "p50=excluded.p50, p75=excluded.p75, mean=excluded.mean, vals_json=excluded.vals_json, "
                    "orgs=excluded.orgs, max_org_share=excluded.max_org_share, members_json=excluded.members_json, "
                    "computed_at=datetime('now') WHERE intel_benchmarks.members_json IS NULL",
                    (cohort, metric, week, len(vals), privacy.round_effect(percentile(vals, 25), 3),
                     privacy.round_effect(percentile(vals, 50), 3), privacy.round_effect(percentile(vals, 75), 3),
                     privacy.round_effect(mean(vals), 3), json.dumps(sorted(vals)), orgs, round(share, 3),
                     json.dumps([[v, o] for v, o in pairs])))
                written += 1
            # One commit per metric (R2-18): a pass that fails part-way keeps
            # what it wrote, and a re-run skips it (frozen) and carries on.
            conn.commit()
    finally:
        conn.close()
    return {"written": written, "week": week, "skipped": skipped, "groups": info}


def _row(cohort, metric, db_path=DB_PATH, today: date = None, with_vals=False):
    cols = "cohort, metric, week, n, p25, p50, p75, mean, computed_at" + (
        ", vals_json, members_json, orgs, max_org_share" if with_vals else "")
    conn = get_conn(db_path)
    try:
        row = conn.execute(f"SELECT {cols} FROM intel_benchmarks WHERE cohort=? AND metric=? AND week >= ? "
                           "ORDER BY week DESC LIMIT 1", (cohort, metric, _week_floor(today))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def band(cohort: str, metric: str, db_path=DB_PATH, today: date = None) -> dict | None:
    """The latest stored band for a cohort within MAX_BAND_AGE_WEEKS, or
    None. Internal (staffing's starting headcount scales its median); what
    an owner is shown goes through published()."""
    row = _row(cohort, metric, db_path=db_path, today=today)
    if not row:
        return None
    row["as_of"] = _as_of(row)
    return privacy.assert_anonymous(row)


def _excluded(exclude_org) -> frozenset:
    """exclude_org as a set of org hashes (one hash, or viewer_org()'s set)."""
    if exclude_org is None:
        return frozenset()
    if isinstance(exclude_org, str):
        return frozenset((exclude_org,))
    return frozenset(exclude_org)


def cap_organisations(pairs, week=None):
    """(kept pairs, dropped) — no organisation over MAX_ORG_SHARE of a band,
    by leaving out its surplus locations rather than withholding the band
    from everyone (fix round #35, R2-10: the first 6-location customer in a
    format blocked peers for every independent in it). Which of an
    organisation's locations sit out is fixed per ISO week (a hash of the
    week and the member), so every viewer of one week's band sees the same
    subset."""
    import hashlib
    per = {}
    for i, (v, o) in enumerate(pairs):
        rank = hashlib.sha256(f"{week}|{o}|{v}|{i}".encode()).hexdigest()
        per.setdefault(o, []).append((rank, v))
    for o in per:
        per[o].sort()
    dropped = 0
    n = len(pairs)
    while n:
        o, members = max(per.items(), key=lambda kv: (len(kv[1]), kv[0]))
        if len(members) / float(n) <= privacy.MAX_ORG_SHARE + 1e-9:
            break
        members.pop()
        n -= 1
        dropped += 1
    kept = [(v, o) for o, members in per.items() for _r, v in members]
    return kept, dropped


def publish_row(row, metric, exclude_org=None, exclude_value=None) -> dict:
    """published() on a stored band row already read (with members_json):
    the pure half, so the nightly ledger (jobs.record_assignments) derives
    its rungs from exactly what a viewer would be shown."""
    cohort = row.get("cohort")
    if not publishable_group(cohort):
        return {"withheld": True, "n": 0, "reason": "an untyped or 'other' group is never published"}
    # The all-types guard in the reader too, not only the writer (R4-28).
    if cohort == "platform" and not _reg.platform_allowed(metric):
        return {"withheld": True, "n": 0,
                "reason": "this measure depends on the type of restaurant, so an all-types band is never shown"}
    # A band is shown to a viewer with their organisation taken out — never
    # to no one in particular (fix round #41, R1-11: the facade published a
    # band with the viewer still in it).
    ex = _excluded(exclude_org)
    if not ex:
        return {"withheld": True, "n": 0,
                "reason": "a band is shown only to a restaurant, with its own organisation left out"}
    members = None
    try:
        members = json.loads(row.get("members_json") or "null")
    except Exception:
        members = None
    if members is None:
        # A band stored before organisations were kept: the viewer's
        # organisation cannot be taken out of it, so it is never shown.
        return {"withheld": True, "n": int(row.get("n") or 0),
                "reason": "this band predates the privacy rules and is recomputed with next week's figures"}
    pairs = [(float(v), o) for v, o in members if o not in ex]
    if exclude_value is not None:
        ev = round(float(exclude_value), 3)
        for i, (v, _o) in enumerate(pairs):
            if abs(v - ev) <= 0.0005:
                del pairs[i]
                break
    measured = len(pairs)
    pairs, dropped = cap_organisations(pairs, row.get("week"))
    n = len(pairs)
    orgs, _share = privacy.org_counts([o for _v, o in pairs])
    if n < MIN_QUARTILE_N:
        why = f"fewer than {MIN_QUARTILE_N} other restaurants have this measured"
        if dropped and measured >= MIN_QUARTILE_N:
            why = (f"one owner's locations would be over a third of the group, and holding them to a third "
                   f"leaves fewer than {MIN_QUARTILE_N} other restaurants")
        return {"withheld": True, "n": n, "measured": measured, "reason": why}
    if orgs < privacy.MIN_ORGS:
        return {"withheld": True, "n": n, "measured": measured,
                "reason": f"the other restaurants come from fewer than {privacy.MIN_ORGS} separate owners"}
    vals = sorted(v for v, _o in pairs)
    p25, p50, p75 = (harrell_davis(vals, 25), harrell_davis(vals, 50), harrell_davis(vals, 75))
    if not _reg.spread_ok(metric, p25, p50, p75):
        return {"withheld": True, "n": n, "measured": measured,
                "reason": "these restaurants' figures are too spread out for a middle to mean anything"}
    out = {"cohort": cohort, "cohort_label": cohort_label(cohort), "metric": metric,
           "week": row.get("week"), "as_of": _as_of(row), "n": n, "orgs": orgs, "measured": measured,
           "capped": dropped,
           "p25": coarse(metric, p25), "p50": coarse(metric, p50), "p75": coarse(metric, p75)}
    return privacy.assert_anonymous(out)


def published(cohort: str, metric: str, exclude_value=None, db_path=DB_PATH, today: date = None,
              exclude_org=None) -> dict | None:
    """The band as it may be shown: None when nothing current exists,
    {withheld: True, n, reason} when it may not be, else {cohort,
    cohort_label, metric, week, as_of, n, orgs, measured, capped, p25, p50,
    p75}.

    `exclude_org` — the viewer (viewer_org(): its organisation's hashes, or
    one privacy.org_hash) — is REQUIRED: the viewer's whole organisation
    comes out, and with no viewer nothing is shown (#41). `exclude_value`
    (older callers) also takes one instance of the viewer's own figure out.
    Then an organisation over a third is held to a third (#35); at least
    MIN_QUARTILE_N others from at least privacy.MIN_ORGS organisations
    (#9); never an `other`, untyped or catch-all group, never an all-types
    band for a type-dependent measure, never a band too spread to mean
    "alike" (#39); quartiles by Harrell–Davis at the metric's coarse step
    (#42). `measured` is how many others had it before the cap, `capped`
    how many locations the cap left out — counts only."""
    if not publishable_group(cohort):
        return {"withheld": True, "n": 0, "reason": "an untyped or 'other' group is never published"}
    row = _row(cohort, metric, db_path=db_path, today=today, with_vals=True)
    if not row:
        return None
    return publish_row(row, metric, exclude_org=exclude_org, exclude_value=exclude_value)


def viewer_org(restaurant_id, db_path=DB_PATH) -> frozenset:
    """The viewer's organisation — what published() leaves out — as a set of
    org hashes: the organisation's key (privacy.org_members: organisation,
    owner email, shared owner logins, Stripe customer), and each of its
    restaurants' own and pre-fix-round keys, so a band frozen earlier in the
    week under an older key still leaves the viewer's organisation out."""
    try:
        canon, rows = privacy.org_members(restaurant_id, db_path=db_path)
    except Exception:
        canon, rows = f"r{restaurant_id}", []
    keys = {canon, f"r{restaurant_id}"}
    for r in rows:
        keys.add(privacy.org_key(r))
        keys.add(privacy.legacy_org_key(r))
    return frozenset(privacy.org_hash(k) for k in keys)


def group_size(cohort, week, exclude_org=None, db_path=DB_PATH) -> int:
    """The most restaurants in `cohort` that measured any benchmarked metric
    in `week`, the viewer's organisation left out — the group's size as the
    band the viewer is shown counts it (R1-15: `members` counted the viewer
    while `n` did not)."""
    ex = _excluded(exclude_org)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT n, members_json FROM intel_benchmarks WHERE cohort=? AND week=?",
                            (cohort, week)).fetchall()
    finally:
        conn.close()
    best = 0
    for r in rows:
        try:
            members = json.loads(r["members_json"] or "null")
        except Exception:
            members = None
        if members is None:
            continue
        best = max(best, sum(1 for _v, o in members if o not in ex))
    return best


def _own(restaurant_id, metric, db_path=DB_PATH, today: date = None, band_week: str = None):
    """(value now, value at the band's week, own week, stale) — this
    restaurant's figure within MAX_OWN_AGE_WEEKS, and the one that went into
    a band computed in `band_week` (the latest row at most three weeks
    before it — latest_by_restaurant's rule)."""
    rows = _features.series(restaurant_id, weeks=20, db_path=db_path)
    floor = _week_floor(today, MAX_OWN_AGE_WEEKS)
    cur = rows[-1] if rows else None
    value, own_week, stale = None, None, False
    if cur:
        own_week = cur["week"]
        if cur["week"] >= floor:
            value = (cur.get("features") or {}).get(metric)
        else:
            stale = True
    at_band = None
    if band_week:
        mon = _week_monday(band_week)
        lo = _features.iso_week(mon - timedelta(weeks=3)) if mon else None
        for r in reversed(rows):
            if r["week"] <= band_week and (lo is None or r["week"] >= lo):
                at_band = (r.get("features") or {}).get(metric)
                break
    return value, at_band, own_week, stale


def peer_cohort(restaurant, metric):
    """(partition key, why_not) — the owner-confirmed peer partition a
    metric is compared within, or None and the reason."""
    prof = categories.profile_for(restaurant) if restaurant is not None else None
    if not prof or not prof.get("confirmed"):
        return None, "this restaurant's profile isn't confirmed yet, so there is no like-for-like group"
    fam = _reg.partition_family(metric)
    key = categories.partition_key(prof, fam)
    if not key:
        return None, categories.food_why_not(prof)
    return key, None


def peer_ladder(restaurant, metric, structural=None):
    """(ladder, base, why_not) — every group the metric may be compared
    within, finest → coarsest (categories.partition_ladder over the
    restaurant's measured coordinates), the profile's own partition, or
    ([], None, reason). A staffing ratio reads its own family's keys (the
    volume-band rung first, then the pooled partition — R1-14/R2-14/R4-29),
    never the labor key."""
    key, why = peer_cohort(restaurant, metric)
    if not key:
        return [], None, why
    prof = categories.profile_for(restaurant)
    ladder = categories.partition_ladder(prof, _reg.partition_family(metric), structural)
    return (ladder or [key]), key, None


def benchmark(restaurant_id: int, metric: str, cohort: str = None, db_path=DB_PATH,
              cohort_source: str = None, today: date = None, restaurant=None) -> dict:
    """This restaurant against its like-for-like peers — the wrapper every
    older caller keeps. {available, value, cohort, cohort_label, inferred,
    n, p25, p50, p75, week, as_of, own_week, standing} — or {available:
    False, reason}.

    The peer group is the restaurant's CONFIRMED partition (a type string
    passed by an older caller is replaced by it; an explicit partition key
    or a test cohort key is used as given). A guessed type compares with no
    one. Only a behaviour metric falls back to all restaurants on Cavnar;
    labor %, food cost, hours per $1k, waste and rating answer "no
    like-for-like peers yet" instead (#6)."""
    if restaurant is None:
        try:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path) if db_path != DB_PATH \
                else _models_mod.get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    why_none = None
    if cohort_source == "inferred":
        cohort, why_none = None, ("the type was guessed from the restaurant's name — confirm it in Account → "
                                  "Restaurant profile to compare with restaurants like it")
    elif cohort is None or categories.valid(cohort):
        key, why = peer_cohort(restaurant, metric)
        if key:
            cohort = key
        elif cohort is None or restaurant is None or categories.category_for(restaurant)[1] != "set":
            cohort, why_none = None, why
        # else: an owner-set type with no confirmed profile yet — the type
        # cohort an older caller named is used as given.
    org = viewer_org(restaurant_id, db_path=db_path)
    value = None
    out = None
    used = None
    last_reason = why_none
    rungs = [cohort] if cohort else []
    if cohort and restaurant is not None and categories.is_partition(cohort):
        try:
            own_row = _features.latest(restaurant_id, db_path=db_path)
        except Exception:
            own_row = None
        ladder, base, _w = peer_ladder(restaurant, metric, (own_row or {}).get("features"))
        if base == cohort and ladder:
            rungs = ladder
    chain = rungs + (["platform"] if _reg.platform_allowed(metric) else [])
    for c in chain:
        row = _row(c, metric, db_path=db_path, today=today)
        v_now, v_at, own_week, own_stale = _own(restaurant_id, metric, db_path=db_path, today=today,
                                                band_week=(row or {}).get("week"))
        value = v_now
        p = published(c, metric, exclude_org=org, db_path=db_path, today=today) if row else None
        if p and not p.get("withheld"):
            out, used = p, c
            break
        if p and p.get("withheld"):
            last_reason = p.get("reason")
    if not chain:
        v_now, _v_at, _w, _s = _own(restaurant_id, metric, db_path=db_path, today=today)
        value = v_now
    if not out:
        if not cohort and not _reg.platform_allowed(metric):
            last_reason = "no like-for-like peers yet — " + (why_none or "this restaurant's type isn't confirmed")
        return {"available": False, "metric": metric, "value": value,
                "reason": last_reason or (f"no like-for-like peers yet: no current band from at least "
                                          f"{MIN_QUARTILE_N} other restaurants with this measured")}
    inferred = bool(used != "platform" and cohort_source == "inferred")
    res = {"available": True, "metric": metric, "label": LABELS.get(metric, metric), "value": value,
           "cohort": used, "cohort_label": out["cohort_label"], "cohort_source": ("platform" if used == "platform"
                                                                                 else (cohort_source or "set")),
           "inferred": inferred, "n": out["n"], "orgs": out.get("orgs"), "p25": out["p25"], "p50": out["p50"],
           "p75": out["p75"], "better": BETTER.get(metric, "higher"), "week": out["week"], "as_of": out["as_of"],
           "own_week": own_week, "own_stale": own_stale}
    if value is None:
        res["standing"] = "unmeasured"
        return res
    better_high = BETTER.get(metric, "higher") == "higher"
    b = out
    if (value >= b["p75"]) if better_high else (value <= b["p25"]):
        res["standing"] = "top quarter"
    elif (value >= b["p50"]) if better_high else (value <= b["p50"]):
        res["standing"] = "above the middle"
    elif (value >= b["p25"]) if better_high else (value <= b["p75"]):
        res["standing"] = "below the middle"
    else:
        res["standing"] = "bottom quarter"
    return res


def all_for(restaurant_id: int, cohort: str = None, db_path=DB_PATH, cohort_source: str = None) -> list:
    restaurant = None
    try:
        restaurant = _models_mod.get_restaurant(restaurant_id, db_path) if db_path != DB_PATH \
            else _models_mod.get_restaurant(restaurant_id)
    except Exception:
        pass
    return [benchmark(restaurant_id, m, cohort=cohort, db_path=db_path, cohort_source=cohort_source,
                      restaurant=restaurant) for m in _features.BENCHMARK_KEYS]


def context_line(b) -> str | None:
    """One prompt line for a published band, carrying its cohort, size,
    as-of date and — for an inferred type — that it was inferred."""
    if not b or not b.get("available") or b.get("standing") in (None, "unmeasured"):
        return None
    who = ("other restaurants on Cavnar AI — all types, not a like-for-like cohort" if b.get("cohort") == "platform"
           else f"other {b['cohort_label']}")
    line = (f"{b['label']}: this restaurant is in the {b['standing']} of {b['n']} {who} "
            f"(band {b['p25']:g}–{b['p75']:g}, middle {b['p50']:g}; band as of {b.get('as_of') or 'unknown'}")
    mon = _week_monday(b.get("own_week"))
    if mon:
        from time_utils import mdy
        line += f"; this restaurant's figure from the week of {mdy(mon)}"
    line += ")"
    if b.get("inferred"):
        line += " — the type was inferred from the restaurant's name, not set by the owner"
    return line + "."


def cohort_table(db_path=DB_PATH) -> list:
    """Every stored Cavnar cohort band, latest week — for the admin page
    (is_admin only, #47). Rounded as published (the metric's coarse step),
    with n and organisations: an admin reads what an owner could be shown,
    never an individual restaurant's figure at n = 5."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT b.cohort, b.metric, b.week, b.n, b.orgs, b.p25, b.p50, b.p75 FROM intel_benchmarks b "
                            "JOIN (SELECT cohort, metric, MAX(week) AS week FROM intel_benchmarks GROUP BY cohort, metric) m "
                            "ON m.cohort=b.cohort AND m.metric=b.metric AND m.week=b.week ORDER BY b.cohort, b.metric").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for q in ("p25", "p50", "p75"):
            d[q] = coarse(d["metric"], d[q])
        d["label"] = LABELS.get(r["metric"], r["metric"])
        d["cohort_label"] = cohort_label(r["cohort"])
        out.append(privacy.assert_anonymous(d))
    return out
