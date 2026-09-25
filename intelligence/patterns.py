"""Level 2 and 3: pattern discovery across restaurants.

A hypothesis is data: a behaviour feature and the split that defines
"does it", an outcome feature, the direction that counts as better, and
a sentence template. Discovery runs every hypothesis for every peer group
that clears the floor (and platform-wide), and a pattern is written only
when:

  (The groups are the owner-CONFIRMED peer partitions the bands use —
  each hypothesis inside the partition of its own metric family — over the
  members a band may have: jobs.eligible_members, and never a $26/hr
  default-wage restaurant for a labor-cost outcome. A restaurant with no
  confirmed group is served only the all-types patterns. Benchmarking
  re-audit #20, #21.)


  * each side of the split has ≥ MIN_GROUP restaurants,
  * |Cohen's d| ≥ MIN_EFFECT_D,
  * the seeded two-sided permutation p ≤ MAX_P, and
  * the Benjamini–Hochberg q across the night's hypotheses ≤ MAX_Q.

A pattern that stops meeting the bar is retired, not deleted — the
dashboard shows what used to hold. Sentences carry counts and effects;
they never carry a name, and `privacy.assert_anonymous` runs on every row
before it is stored.

Every hypothesis compares restaurants' latest rows side by side: the
behaviour and the outcome are measured over the SAME weeks, so a sentence
says "at the same time as", never "over the following" (BM1-16, BM4-5). A
pattern found across every restaurant on Cavnar ("platform") pools every
type of restaurant and is marked `pooled_types`; such a pattern never
supports a recommendation about an economics metric (labor %, food cost %,
waste — metrics_registry), where a type difference would pass as a
behaviour effect (pooled_on_economics).

What an OWNER (or a model answering one) is served is a narrower projection
(Benchmarking audit #11, BM1-8): `active()` strips the group means
(`mean_with` / `mean_without` — with a side of five and your own figure,
the other four's sum falls out) and serves only a pattern with at least
MIN_ORGS_PER_SIDE organisations on each side of the split. The admin
projection (`all_patterns`, `active(projection="admin")`) keeps them. The
figures are frozen for the ISO week: a nightly re-run that confirms a
pattern re-dates it but does not move its numbers until the next week, so a
member joining or leaving cannot be differenced out of two nights.
"""
import json
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy, categories
from . import features as _features
from .stats import cohen_d, permutation_test, benjamini_hochberg, mean


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


MIN_EFFECT_D = 0.3
MAX_P = 0.05
MAX_Q = 0.10
SHUFFLES = 2000
# Organisations on each side of a split before an owner is shown the pattern.
MIN_ORGS_PER_SIDE = 8
# Evidence keys an owner-facing projection never carries.
ADMIN_ONLY_EVIDENCE = ("mean_with", "mean_without")

# behaviour: (feature, op, threshold). outcome: feature. better: "higher"|"lower".
HYPOTHESES = (
    {"key": "reply_fast_rating", "behaviour": ("response_24h_rate_30d", ">=", 0.5), "outcome": "avg_rating_delta",
     "better": "higher", "unit": "★",
     "sentence": "those replying to at least half their reviews within a day saw their rating move {effect_abs:.2f}★ {direction} than those that did not, measured at the same time as the replying (not after it)",
     "rec_kinds": ("reply", "respond", "reviews", "urgent")},
    {"key": "reply_rate_rating", "behaviour": ("reply_rate_30d", ">=", 0.8), "outcome": "avg_rating_30d",
     "better": "higher", "unit": "★",
     "sentence": "those replying to four in five reviews averaged {effect_abs:.2f}★ {direction}",
     "rec_kinds": ("reply", "respond", "reviews")},
    {"key": "adjust_schedule_variance", "behaviour": ("schedule_adjust_rate", ">=", 0.5), "outcome": "labor_pct_sd_28d",
     "better": "lower", "unit": "pts",
     "sentence": "those adjusting at least half their weekly schedules before publishing ran {effect_abs:.1f} points {direction} day-to-day labor variance",
     "rec_kinds": ("trim_day", "schedule", "labor", "observed:schedule_published")},
    {"key": "adjust_schedule_labor", "behaviour": ("schedule_adjust_rate", ">=", 0.5), "outcome": "labor_pct_28d",
     "better": "lower", "unit": "%",
     "sentence": "those adjusting at least half their weekly schedules before publishing ran labor {effect_abs:.1f} points {direction}",
     "rec_kinds": ("trim_day", "schedule", "labor")},
    {"key": "dish_posts_lift", "behaviour": ("dish_posts_28d", ">=", 2), "outcome": "post_lift_median_28d",
     "better": "higher", "unit": "%",
     "sentence": "those posting about a specific dish at least twice a month saw {effect_abs:.1f} points {direction} median sales lift after their posts",
     "rec_kinds": ("post_this_week", "first_post", "post", "marketing", "observed:post_published")},
    {"key": "occasion_posts_engagement", "behaviour": ("occasion_posts_28d", ">=", 1), "outcome": "post_engagement_rate_28d",
     "better": "higher", "unit": "share",
     "sentence": "those tying at least one post a month to a game, holiday or event drew {effect_pct:.1f} points {direction} engagement rates",
     "rec_kinds": ("post_this_week", "post", "marketing")},
    {"key": "offer_posts_lift", "behaviour": ("offer_posts_28d", ">=", 1), "outcome": "post_lift_median_28d",
     "better": "higher", "unit": "%",
     "sentence": "those posting at least one offer a month saw {effect_abs:.1f} points {direction} median sales lift after their posts",
     "rec_kinds": ("post", "marketing", "draft_campaign")},
    {"key": "post_cadence_lift", "behaviour": ("posts_28d", ">=", 4), "outcome": "post_lift_median_28d",
     "better": "higher", "unit": "%",
     "sentence": "those publishing weekly or more saw {effect_abs:.1f} points {direction} median sales lift per post",
     "rec_kinds": ("post_this_week", "first_post", "post")},
    {"key": "campaign_cadence_return", "behaviour": ("campaigns_28d", ">=", 2), "outcome": "campaign_return_rate_28d",
     "better": "higher", "unit": "share",
     "sentence": "those texting their list at least twice a month had {effect_pct:.0f} points {direction} return rates within 14 days",
     "rec_kinds": ("draft_campaign", "marketing")},
    {"key": "waste_tracking_food_cost", "behaviour": ("waste_sales_pct_28d", "<=", 2.0), "outcome": "food_cost_pct_28d",
     "better": "lower", "unit": "%",
     "sentence": "those holding waste under 2% of sales ran food cost {effect_abs:.1f} points {direction}",
     "rec_kinds": ("cut_waste", "count", "inventory", "reprice")},
    {"key": "acting_on_recs_improves", "behaviour": ("recs_done_28d", ">=", 1), "outcome": "outcomes_improved_rate_90d",
     "better": "higher", "unit": "share",
     "sentence": "those acting on at least one recommendation a month had {effect_pct:.0f} points {direction} measured-improvement rates",
     "rec_kinds": ()},
    {"key": "weekend_share_labor", "behaviour": ("weekend_sales_share_28d", ">=", 0.55), "outcome": "labor_pct_28d",
     "better": "lower", "unit": "%",
     "sentence": "those taking over 55% of sales Friday to Sunday ran labor {effect_abs:.1f} points {direction} overall",
     "rec_kinds": ("trim_day", "schedule")},
)


def _split(rows, behaviour):
    key, op, thr = behaviour
    with_, without = [], []
    for r in rows:
        v = (r.get("features") or {}).get(key)
        if v is None:
            continue
        ok = {">=": v >= thr, "<=": v <= thr, ">": v > thr, "<": v < thr}[op]
        (with_ if ok else without).append(r)
    return with_, without


# Bounds (Benchmarking audit BM4-14, Top-50 #46). Discovery walks cohorts in
# name order from a cursor in job_cursors and stops at the wall clock; the
# next night resumes after the last cohort it finished, so the same tail is
# never starved. A hypothesis whose p is clearly above MAX_P stops shuffling
# early (Besag–Clifford sequential stop): at every EARLY_STOP_EVERY shuffles,
# once EARLY_STOP_HITS shuffled differences have matched the observed one,
# p is at least that share and nothing more is learned by continuing.
DISCOVER_WALL_SECONDS = 120
CURSOR_KEY = "intelligence_patterns"
EARLY_STOP_EVERY = 200
EARLY_STOP_HITS = 20


def permutation_test_sequential(a, b, shuffles=SHUFFLES, seed=7, every=EARLY_STOP_EVERY, stop_hits=EARLY_STOP_HITS):
    """stats.permutation_test with a sequential stop: (observed, p, shuffles
    run). Same seed and the same shuffle sequence, so a test that runs to the
    end returns exactly permutation_test's p."""
    import random
    a = [float(x) for x in a if x is not None]
    b = [float(x) for x in b if x is not None]
    if not a or not b:
        return None, None, 0
    observed = mean(a) - mean(b)
    pool = a + b
    na = len(a)
    rng = random.Random(seed)
    hits = done = 0
    for _ in range(shuffles):
        rng.shuffle(pool)
        diff = mean(pool[:na]) - mean(pool[na:])
        if abs(diff) >= abs(observed) - 1e-12:
            hits += 1
        done += 1
        if every and done % every == 0 and hits >= stop_hits:
            break
    return observed, (hits + 1) / (done + 1), done


def test_hypothesis(rows, h, shuffles=SHUFFLES) -> dict | None:
    """One hypothesis over one cohort's latest rows. None when a side is
    too small; otherwise a candidate with its statistics (not yet judged).
    A row carrying `_org` (discover() adds it) is counted by organisation."""
    with_, without = _split(rows, h["behaviour"])
    a_rows = [r for r in with_ if r["features"].get(h["outcome"]) is not None]
    b_rows = [r for r in without if r["features"].get(h["outcome"]) is not None]
    a = [r["features"][h["outcome"]] for r in a_rows]
    b = [r["features"][h["outcome"]] for r in b_rows]
    if len(a) < privacy.MIN_GROUP or len(b) < privacy.MIN_GROUP:
        return None
    diff, p, ran = permutation_test_sequential(a, b, shuffles=shuffles)
    d = cohen_d(a, b)
    return {"key": h["key"], "n_with": len(a), "n_without": len(b), "effect": diff, "p_value": p, "cohen_d": d,
            "mean_with": mean(a), "mean_without": mean(b), "shuffles_run": ran,
            "orgs_with": len({r.get("_org") or id(r) for r in a_rows}),
            "orgs_without": len({r.get("_org") or id(r) for r in b_rows})}


# ── prospective mode (BM4-5, BM1-16; Top-50 #45) ─────────────────────────────
#
# The cross-sectional hypotheses above compare different restaurants in the
# same week: nothing in them FOLLOWED anything. A prospective hypothesis
# reads the behaviour from a restaurant's week-t feature row and the outcome
# as the change of the outcome feature from week t to week t + HORIZON_WEEKS
# (the intel_features series), one pair per restaurant (its latest complete
# pair). The permutation shuffles "did it / didn't" WITHIN each restaurant
# type, and only types with MIN_GROUP on each side contribute, so pooling a
# coffee shop with a sports bar cannot manufacture an effect (Simpson's
# paradox). Stored with `prospective: true` and `pooled_types`. Dormant
# until a type has MIN_GROUP restaurants on each side with 13 weeks between
# their rows — every type today.
HORIZON_WEEKS = 13
PROSPECTIVE_HYPOTHESES = (
    {"key": "prospective_reply_fast_rating", "behaviour": ("response_24h_rate_30d", ">=", 0.5),
     "outcome": "avg_rating_30d", "better": "higher", "unit": "★",
     "did": "replied to at least half their reviews within a day", "outcome_text": "their average rating",
     "rec_kinds": ("reply", "respond", "reviews", "urgent")},
    {"key": "prospective_adjust_schedule_labor", "behaviour": ("schedule_adjust_rate", ">=", 0.5),
     "outcome": "labor_pct_28d", "better": "lower", "unit": " pts",
     "did": "adjusted at least half their weekly schedules before publishing", "outcome_text": "labor %",
     "rec_kinds": ("trim_day", "schedule", "labor")},
    {"key": "prospective_waste_food_cost", "behaviour": ("waste_sales_pct_28d", "<=", 2.0),
     "outcome": "food_cost_pct_28d", "better": "lower", "unit": " pts",
     "did": "held logged waste under 2% of sales (logging it regularly)", "outcome_text": "food cost %",
     "rec_kinds": ("cut_waste", "count", "inventory")},
    {"key": "prospective_post_cadence_lift", "behaviour": ("posts_28d", ">=", 4),
     "outcome": "post_lift_median_28d", "better": "higher", "unit": " pts",
     "did": "published weekly or more", "outcome_text": "the median sales lift after a post",
     "rec_kinds": ("post_this_week", "first_post", "post")},
    {"key": "prospective_acting_on_recs", "behaviour": ("recs_done_28d", ">=", 1),
     "outcome": "outcomes_improved_rate_90d", "better": "higher", "unit": " pts",
     "did": "acted on at least one recommendation in a month", "outcome_text": "their measured-improvement rate",
     "rec_kinds": ()},
)


def _week_plus(week, weeks):
    y, w = str(week).split("-W")
    return _features.iso_week(date.fromisocalendar(int(y), int(w), 1) + timedelta(weeks=weeks))


def prospective_pairs(db_path=DB_PATH, weeks_back=HORIZON_WEEKS + 12) -> dict:
    """{restaurant_id: {"t": features at week t, "t_h": features at week t +
    HORIZON_WEEKS, "week": t}} — each real restaurant's latest complete pair,
    from the cross-restaurant view of the series (waste gated, demo
    accounts out)."""
    by_week = _features.weekly_by_restaurant(weeks=weeks_back, db_path=db_path)
    per = {}
    for wk, rows in by_week.items():
        for rid, f in rows.items():
            per.setdefault(rid, {})[wk] = f
    out = {}
    for rid, series in per.items():
        for wk in sorted(series, reverse=True):
            try:
                later = _week_plus(wk, HORIZON_WEEKS)
            except (ValueError, TypeError):
                continue
            if later in series:
                out[rid] = {"t": series[wk], "t_h": series[later], "week": wk}
                break
    return out


def _stratified_effect(strata):
    """Σ_s c_s (mean_with_s − mean_without_s) ÷ Σ_s c_s with c_s =
    n_w·n_wo ÷ (n_w + n_wo) — a Mantel–Haenszel-style weighted difference."""
    num = den = 0.0
    for a, b in strata:
        c = len(a) * len(b) / float(len(a) + len(b))
        num += c * (sum(a) / len(a) - sum(b) / len(b))
        den += c
    return num / den if den else None


def test_prospective(pairs, h, strata_of, shuffles=SHUFFLES, seed=7, org_of=None) -> dict | None:
    """One prospective hypothesis over {rid: pair}: behaviour at week t,
    outcome = change to week t + HORIZON_WEEKS, permuted within strata
    (strata_of(rid) → the restaurant's type). None when no stratum has
    MIN_GROUP restaurants on each side. `org_of(rid)` counts each side by
    organisation (#11)."""
    import random
    feat, op, thr = h["behaviour"]
    by = {}
    side_rids = {}
    for rid, p in pairs.items():
        x = (p["t"] or {}).get(feat)
        y0, y1 = (p["t"] or {}).get(h["outcome"]), (p["t_h"] or {}).get(h["outcome"])
        if x is None or y0 is None or y1 is None:
            continue
        ok = {">=": x >= thr, "<=": x <= thr, ">": x > thr, "<": x < thr}[op]
        key = strata_of(rid) or "untyped"
        s = by.setdefault(key, ([], []))
        (s[0] if ok else s[1]).append(float(y1) - float(y0))
        side_rids.setdefault(key, ([], []))[0 if ok else 1].append(rid)
    kept = [k for k, (a, b) in by.items() if len(a) >= privacy.MIN_GROUP and len(b) >= privacy.MIN_GROUP]
    strata = [by[k] for k in kept]
    if not strata:
        return None
    observed = _stratified_effect(strata)
    rng = random.Random(seed)
    hits = done = 0
    for _ in range(shuffles):
        shuffled = []
        for a, b in strata:
            pool = a + b
            rng.shuffle(pool)
            shuffled.append((pool[:len(a)], pool[len(a):]))
        if abs(_stratified_effect(shuffled)) >= abs(observed) - 1e-12:
            hits += 1
        done += 1
        if done % EARLY_STOP_EVERY == 0 and hits >= EARLY_STOP_HITS:
            break
    all_a = [v for a, _b in strata for v in a]
    all_b = [v for _a, b in strata for v in b]
    return {"key": h["key"], "n_with": len(all_a), "n_without": len(all_b), "effect": observed,
            "p_value": (hits + 1) / (done + 1), "cohen_d": cohen_d(all_a, all_b), "mean_with": mean(all_a),
            "mean_without": mean(all_b), "strata": len(strata), "shuffles_run": done, "prospective": True,
            "orgs_with": len({(org_of or (lambda r: r))(r) for k in kept for r in side_rids[k][0]}),
            "orgs_without": len({(org_of or (lambda r: r))(r) for k in kept for r in side_rids[k][1]})}


def _prospective_sentence(h, cand, cohort_label, n_total):
    eff = cand["effect"]
    direction = "higher" if eff > 0 else "lower"
    size = f"{abs(eff):.2f}" if h["unit"] == "★" else f"{abs(eff):.1f}"
    who = cohort_label[:1].lower() + cohort_label[1:]
    return (f"Across {n_total} {who}, those that {h['did']} saw {h['outcome_text']} end "
            f"{size}{h['unit']} {direction} over the following {HORIZON_WEEKS} weeks than those that did not — "
            f"measured after the behaviour, compared within each type; an association, not proof of cause.")


def _sentence(h, cand, cohort_label, n_total):
    better = h["better"]
    eff = cand["effect"]
    direction = "higher" if eff > 0 else "lower"
    text = h["sentence"].format(effect_abs=abs(eff), effect_pct=abs(eff) * 100, direction=direction)
    return f"Across {n_total} {cohort_label[:1].lower() + cohort_label[1:]}, {text}."


def _confidence(cand) -> float:
    """0..1 from effect size, p and n — a reader's shorthand, not a p.

    PATTERN STRENGTH, not the probability the pattern is right (CA1 red
    flag 9, CA2 finding 2): 0.4 × effect size (|Cohen's d| ÷ 0.8, capped at
    1 — 0.8 is a "large" effect), 0.35 × how far p sits under MAX_P, 0.25 ×
    sample (n ÷ 40, capped at 1). The weights order patterns for display;
    they were never fitted to anything. Payloads carry it as `strength_pct`
    (0–100) with `confidence` kept for older readers (strength_fields)."""
    n = cand["n_with"] + cand["n_without"]
    size = min(1.0, abs(cand["cohen_d"] or 0) / 0.8)
    sig = max(0.0, 1.0 - (cand["p_value"] or 1) / MAX_P)
    scale = min(1.0, n / 40.0)
    return round(0.4 * size + 0.35 * sig + 0.25 * scale, 3)


STRENGTH_LABEL = "pattern strength"
STRENGTH_BASIS = ("effect size, significance and sample combined for ordering — not the chance the pattern is "
                  "right")


def strength_fields(d) -> dict:
    """A pattern row as payloads carry it: `strength_pct` (0–100, the
    _confidence blend), its label and basis, and `confidence` (0–1) kept
    for older readers (CA1 E4 / red flag 9)."""
    try:
        c = float(d.get("confidence"))
    except (TypeError, ValueError):
        c = None
    d["strength_pct"] = int(round(c * 100)) if c is not None else None
    d["strength_label"] = STRENGTH_LABEL
    d["strength_basis"] = STRENGTH_BASIS
    d["pooled_types"] = d.get("cohort") == "platform"
    return d


def pooled_on_economics(p) -> bool:
    """A platform-pooled pattern (every type of restaurant together) about
    an economics metric — labor %, food cost %, waste, hours or staff per
    $1k (metrics_registry) — on either side of its split. It is never
    support for a recommendation: a coffee shop and a steakhouse differ on
    those for reasons that have nothing to do with the behaviour."""
    if not (p or {}).get("pooled_types") and (p or {}).get("cohort") != "platform":
        return False
    from . import metrics_registry as reg
    ev = p.get("evidence") or {}
    beh = ev.get("behaviour") or []
    keys = [ev.get("outcome")] + ([beh[0]] if beh else [])
    return any(reg.comparability(k) == reg.ECONOMICS or str(k or "").startswith(("labor_", "food_cost", "waste_"))
               for k in keys if k)


def _cursor(conn, value=None):
    """The last cohort a bounded discovery pass finished ('' = start over)."""
    if value is None:
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
            return str(row["value"] or "") if row else ""
        except Exception:
            return ""
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                 (CURSOR_KEY, str(value)))


# Which hard split a hypothesis is read inside (categories.partition_key):
# the strictest family of its outcome and its behaviour — a labor outcome
# is read among restaurants that staff alike, a food one among the same
# menu family (Benchmarking re-audit R1-09, R2-8).
_FAMILY_ORDER = {"format": 0, "labor": 1, "food": 2}


def family_of(h) -> str:
    from . import metrics_registry as reg
    keys = [h.get("outcome")] + ([h["behaviour"][0]] if h.get("behaviour") else [])
    fams = [reg.partition_family(k) for k in keys if k]
    fams = [f if f in _FAMILY_ORDER else "labor" for f in fams]
    return max(fams, key=_FAMILY_ORDER.get) if fams else "format"


def _labor_cost(h) -> bool:
    from . import metrics_registry as reg
    return h.get("outcome") in reg.LABOR_COST_METRICS or \
        (bool(h.get("behaviour")) and h["behaviour"][0] in reg.LABOR_COST_METRICS)


def _hypothesis(key):
    return next((h for h in HYPOTHESES + PROSPECTIVE_HYPOTHESES if h["key"] == key), None)


def viewer_cohorts(restaurant) -> dict:
    """{family: partition key} — the confirmed peer groups a restaurant's
    patterns are read from, one per metric family; {} for a profile the
    owner has not confirmed (a guess reads no group's patterns, only the
    all-types ones)."""
    if restaurant is None:
        return {}
    try:
        prof = categories.profile_for(restaurant)
    except Exception:
        return {}
    out = {}
    for fam in categories.FAMILIES:
        k = categories.partition_key(prof, fam)
        if k:
            out[fam] = k
    return out


def _group_label(cohort) -> str:
    if cohort == "platform":
        return "restaurants on Cavnar (all types)"
    if categories.is_partition(cohort):
        return categories.partition_label(cohort)
    return f"{categories.label(cohort).lower()} on Cavnar"


def discover(db_path=DB_PATH, cohorts: dict = None, today: date = None, shuffles=SHUFFLES,
             wall_seconds=DISCOVER_WALL_SECONDS, members: dict = None, partitions: dict = None) -> dict:
    """Run every hypothesis over every peer group that clears the floor,
    plus platform-wide — cross-sectional and prospective.

    The members are the ones a band may have (Benchmarking re-audit #21,
    R1-09 / R2-8): jobs.eligible_members — live MIN_LIVE_WEEKS, half its
    measures on file, one per Google listing, no test account — and a
    labor-cost outcome never counts a restaurant on the $26/hr default
    wage. The groups are the owner-CONFIRMED peer partitions
    (`partitions`, jobs.peer_partitions; read here when not given), each
    hypothesis inside the partition of its own metric family (family_of),
    never the restaurant type. `cohorts` (a type map) is no longer a
    grouping; it is accepted so older callers keep working. `members`
    (jobs.member_info, read here when not given) counts each side of a
    split by organisation (#11).

    Bounded and resumable (CLAUDE.md): groups run in name order from the
    cursor, the first one always runs, and once `wall_seconds` has passed the
    pass stops and records the last group it finished. Only groups actually
    tested tonight can have a pattern retired for failing; a group below the
    floor is retired whether tested or not."""
    import time
    from . import jobs as _jobs
    latest = _features.latest_by_restaurant(db_path=db_path)
    if members is None:
        members = _jobs.member_info(db_path=db_path, today=today)
    members_info = members or {}
    if partitions is None:
        partitions = _jobs.peer_partitions(members_info, latest=latest, db_path=db_path, today=today)
    partitions = partitions or {}
    elig, _skipped = _jobs.eligible_members(latest, members_info)

    def _org(rid):
        return (members_info.get(rid) or {}).get("org_hash") or privacy.org_hash(f"r{rid}")
    latest = {rid: dict(row, _org=_org(rid), _rid=rid) for rid, row in latest.items() if rid in elig}
    week = _features.iso_week(today or date.today())
    fam_groups = {fam: {"platform": list(latest)} for fam in _FAMILY_ORDER}
    for rid in latest:
        for fam in _FAMILY_ORDER:
            key = (partitions.get(rid) or {}).get(fam) if isinstance(partitions.get(rid), dict) else None
            if key:
                fam_groups[fam].setdefault(key, []).append(rid)

    def _format_key(rid):
        entry = partitions.get(rid)
        return entry.get("format") if isinstance(entry, dict) else None

    def _members_for(h, rids):
        if _labor_cost(h):
            return [r for r in rids if (members_info.get(r) or {}).get("cost_basis") != "default"]
        return list(rids)

    eligible = sorted({k for fam, gs in fam_groups.items() for k, rids in gs.items() if privacy.cohort_ok(len(rids))})
    conn = get_conn(db_path)
    try:
        after = _cursor(conn)
    finally:
        conn.close()
    order = [c for c in eligible if c > after] + [c for c in eligible if c <= after]
    try:
        pairs = prospective_pairs(db_path=db_path)
    except Exception as e:
        print(f"[intelligence.patterns] prospective series unavailable: {e}")
        pairs = {}
    deadline = time.monotonic() + float(wall_seconds if wall_seconds is not None else DISCOVER_WALL_SECONDS)
    candidates, tested_now = [], set()
    stopped_early, last_done = False, after
    for i, cohort in enumerate(order):
        if i > 0 and time.monotonic() > deadline:
            stopped_early = True
            break
        for h in HYPOTHESES:
            rids = _members_for(h, fam_groups[family_of(h)].get(cohort) or [])
            if not privacy.cohort_ok(len(rids)):
                continue
            rows = [latest[r] for r in rids]
            cand = test_hypothesis(rows, h, shuffles=shuffles)
            if cand:
                cand["cohort"] = cohort
                cand["n_total"] = len(rows)
                cand["hypothesis"] = h
                candidates.append(cand)
        strata_of = (lambda r: _format_key(r)) if cohort == "platform" else (lambda r, _c=cohort: _c)
        for h in PROSPECTIVE_HYPOTHESES:
            rids = _members_for(h, fam_groups[family_of(h)].get(cohort) or [])
            if not privacy.cohort_ok(len(rids)):
                continue
            group_pairs = {r: pairs[r] for r in rids if r in pairs}
            cand = test_prospective(group_pairs, h, strata_of, shuffles=shuffles, org_of=_org)
            if cand:
                cand["cohort"] = cohort
                cand["n_total"] = cand["n_with"] + cand["n_without"]
                cand["hypothesis"] = h
                candidates.append(cand)
        tested_now.add(cohort)
        last_done = cohort
    qs = benjamini_hochberg([c["p_value"] for c in candidates])
    for c, q in zip(candidates, qs):
        c["q_value"] = q
    active_keys = set()
    written = retired = 0
    conn = get_conn(db_path)
    try:
        frozen = {}
        for r in conn.execute("SELECT key, evidence_json FROM intel_patterns WHERE status='active'").fetchall():
            try:
                frozen[r["key"]] = (json.loads(r["evidence_json"] or "{}") or {}).get("week")
            except Exception:
                frozen[r["key"]] = None
        for c in candidates:
            h = c["hypothesis"]
            direction_ok = (c["effect"] > 0) == (h["better"] == "higher")
            passes = (abs(c["cohen_d"] or 0) >= MIN_EFFECT_D and c["p_value"] is not None and c["p_value"] <= MAX_P
                      and c["q_value"] is not None and c["q_value"] <= MAX_Q and direction_ok)
            if not passes:
                continue
            key = f"{c['cohort']}:{h['key']}"
            # The cohort actually tested, named as such (NS4 H4): a type's
            # label, or every restaurant on Cavnar — never "like yours".
            label = _group_label(c["cohort"])
            row = {"key": key, "cohort": c["cohort"], "hypothesis": h["key"], "n_with": c["n_with"], "n_without": c["n_without"],
                   "effect": privacy.round_effect(c["effect"], 3), "effect_unit": h["unit"],
                   "cohen_d": privacy.round_effect(c["cohen_d"], 3), "p_value": round(c["p_value"], 4),
                   "q_value": round(c["q_value"], 4), "confidence": _confidence(c),
                   "sentence": (_prospective_sentence(h, c, label, c["n_total"]) if c.get("prospective")
                                else _sentence(h, c, label, c["n_total"])),
                   "evidence": {"n": c["n_total"], "mean_with": privacy.round_effect(c["mean_with"], 3),
                                "mean_without": privacy.round_effect(c["mean_without"], 3), "behaviour": list(h["behaviour"]),
                                "outcome": h["outcome"], "rec_kinds": list(h["rec_kinds"]),
                                "orgs_with": c.get("orgs_with"), "orgs_without": c.get("orgs_without"),
                                "week": week,
                                # BM4-5: whether the outcome was measured AFTER
                                # the behaviour, and whether every type was
                                # pooled (platform) — a pooled pattern never
                                # supports a type-sensitive recommendation.
                                "prospective": bool(c.get("prospective")),
                                "pooled_types": c["cohort"] == "platform"}}
            if c.get("prospective"):
                row["evidence"].update(horizon_weeks=HORIZON_WEEKS, strata=c.get("strata"),
                                       outcome_measure="change over the horizon")
            privacy.assert_anonymous(row)
            if frozen.get(key) == week:
                # Frozen for the week (#11): confirmed, re-dated, not re-figured.
                conn.execute("UPDATE intel_patterns SET last_confirmed=datetime('now') WHERE key=?", (key,))
                active_keys.add(key)
                written += 1
                continue
            conn.execute(
                "INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, effect_unit, cohen_d, p_value, "
                "q_value, confidence, sentence, evidence_json, status, last_confirmed, computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active',datetime('now'),datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET n_with=excluded.n_with, n_without=excluded.n_without, effect=excluded.effect, "
                "cohen_d=excluded.cohen_d, p_value=excluded.p_value, q_value=excluded.q_value, confidence=excluded.confidence, "
                "sentence=excluded.sentence, evidence_json=excluded.evidence_json, status='active', "
                "last_confirmed=datetime('now'), computed_at=datetime('now')",
                (row["key"], row["cohort"], row["hypothesis"], row["n_with"], row["n_without"], row["effect"], row["effect_unit"],
                 row["cohen_d"], row["p_value"], row["q_value"], row["confidence"], row["sentence"], json.dumps(row["evidence"])))
            active_keys.add(key)
            written += 1
        # Retire what no longer holds among the cohorts tested tonight — and
        # every pattern whose cohort has dropped below the floor (NS4 H5): it
        # was only ever retired "among cohorts we could test", so a cohort
        # that shrank kept its patterns active, and quoted, indefinitely.
        # Only cohorts TESTED tonight: a cohort the wall clock never reached
        # keeps its patterns until the next pass reaches it (BM4-14).
        tested = set(tested_now)
        for r in conn.execute("SELECT key, cohort, hypothesis FROM intel_patterns WHERE status='active'").fetchall():
            h = _hypothesis(r["hypothesis"])
            fams = [family_of(h)] if h else list(_FAMILY_ORDER)
            below_floor = not any(privacy.cohort_ok(len(fam_groups[f].get(r["cohort"]) or [])) for f in fams)
            if below_floor or (r["cohort"] in tested and r["key"] not in active_keys):
                conn.execute("UPDATE intel_patterns SET status='retired', computed_at=datetime('now') WHERE key=?", (r["key"],))
                retired += 1
        # A completed sweep resets the cursor so the next night starts over.
        _cursor(conn, last_done if stopped_early else "")
        conn.commit()
    finally:
        conn.close()
    return {"cohorts_tested": sorted(tested_now), "candidates": len(candidates), "active": written,
            "retired": retired, "complete": not stopped_early, "resumed_after": after or None}


# An active pattern not re-confirmed within this long is not served: the
# nightly discovery confirms or retires, so an unconfirmed one means the job
# stopped, and a stale association is not quoted as current (NS4 H5).
MAX_PATTERN_AGE_DAYS = 56


def _as_of(raw):
    from time_utils import mdy
    try:
        return mdy(str(raw)[:10])
    except Exception:
        return None


def owner_projection(d) -> dict | None:
    """A pattern as an owner (or a model answering one) may see it: no
    group means, and only with MIN_ORGS_PER_SIDE organisations on each side
    of the split — None otherwise (Benchmarking audit #11)."""
    ev = dict(d.get("evidence") or {})
    try:
        ow, oo = int(ev.get("orgs_with") or 0), int(ev.get("orgs_without") or 0)
    except (TypeError, ValueError):
        ow = oo = 0
    if ow < MIN_ORGS_PER_SIDE or oo < MIN_ORGS_PER_SIDE:
        return None
    for k in ADMIN_ONLY_EVIDENCE:
        ev.pop(k, None)
    out = dict(d)
    out["evidence"] = ev
    return out


def active(cohort=None, db_path=DB_PATH, include_platform=True, limit=20, projection="owner",
           all_cohorts=False) -> list:
    """Active patterns for a peer group, with platform-wide ones after them,
    each re-confirmed within MAX_PATTERN_AGE_DAYS and carrying `as_of`
    (M/D/YY). Rows are anonymous by construction; asserted again on the
    way out. `projection="owner"` (the default — Ask, the prompts, the
    confidence model) applies owner_projection; the admin page passes
    "admin".

    `cohort` is a viewer's {family: partition key} (viewer_cohorts) — a
    pattern is served only from the partition of its own hypothesis's
    family — or one group key. With no cohort only the all-types patterns
    are served (Benchmarking re-audit R2-7: a restaurant with no confirmed
    group read every group's patterns); `all_cohorts=True` is the admin
    view of every group."""
    by_family = dict(cohort) if isinstance(cohort, dict) else None
    keys = sorted(set(by_family.values())) if by_family is not None else ([cohort] if cohort else [])
    fresh = f"AND last_confirmed >= datetime('now', '-{int(MAX_PATTERN_AGE_DAYS)} days')"
    conn = get_conn(db_path)
    try:
        if all_cohorts:
            rows = conn.execute(f"SELECT * FROM intel_patterns WHERE status='active' {fresh} "
                                "ORDER BY confidence DESC LIMIT ?", (int(limit),)).fetchall()
        else:
            want = keys + (["platform"] if include_platform or not keys else [])
            if not want:
                return []
            marks = ",".join("?" for _ in want)
            rows = conn.execute(f"SELECT * FROM intel_patterns WHERE status='active' {fresh} AND cohort IN ({marks}) "
                                "ORDER BY CASE WHEN cohort='platform' THEN 1 ELSE 0 END, confidence DESC LIMIT ?",
                                (*want, int(limit) * (3 if by_family else 1))).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if by_family is not None and d.get("cohort") != "platform":
            h = _hypothesis(d.get("hypothesis"))
            if h is not None and by_family.get(family_of(h)) != d.get("cohort"):
                continue          # another family's group under the same key
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        d.pop("id", None)
        d["as_of"] = _as_of(d.get("last_confirmed"))
        if projection != "admin":
            d = owner_projection(d)
            if d is None:
                continue
        out.append(privacy.assert_anonymous(strength_fields(d)))
        if len(out) >= int(limit):
            break
    return out


def all_patterns(db_path=DB_PATH, limit=100) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM intel_patterns ORDER BY status='active' DESC, confidence DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        d.pop("id", None)
        out.append(privacy.assert_anonymous(strength_fields(d)))
    return out


def support_for(rec_kind: str, cohort=None, db_path=DB_PATH) -> dict | None:
    """The strongest active pattern whose rec_kinds cover this kind — never
    a platform-pooled one about an economics metric (pooled_on_economics)."""
    kind = (rec_kind or "").split(":")[0]
    for p in active(cohort, db_path=db_path):
        if pooled_on_economics(p):
            continue
        kinds = p["evidence"].get("rec_kinds") or []
        if rec_kind in kinds or kind in kinds:
            return p
    return None
