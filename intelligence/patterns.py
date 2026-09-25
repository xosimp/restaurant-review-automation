"""Level 2 and 3: pattern discovery across restaurants.

A hypothesis is data: a behaviour feature and the split that defines
"does it", an outcome feature, the direction that counts as better, and
a sentence template. Discovery runs every hypothesis for every cohort
that clears the floor (and platform-wide), and a pattern is written only
when:

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
"""
import json
from datetime import date

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


def test_hypothesis(rows, h, shuffles=SHUFFLES) -> dict | None:
    """One hypothesis over one cohort's latest rows. None when a side is
    too small; otherwise a candidate with its statistics (not yet judged)."""
    with_, without = _split(rows, h["behaviour"])
    a = [r["features"].get(h["outcome"]) for r in with_ if r["features"].get(h["outcome"]) is not None]
    b = [r["features"].get(h["outcome"]) for r in without if r["features"].get(h["outcome"]) is not None]
    if len(a) < privacy.MIN_GROUP or len(b) < privacy.MIN_GROUP:
        return None
    diff, p = permutation_test(a, b, shuffles=shuffles)
    d = cohen_d(a, b)
    return {"key": h["key"], "n_with": len(a), "n_without": len(b), "effect": diff, "p_value": p, "cohen_d": d,
            "mean_with": mean(a), "mean_without": mean(b)}


def _sentence(h, cand, cohort_label, n_total):
    better = h["better"]
    eff = cand["effect"]
    direction = "higher" if eff > 0 else "lower"
    text = h["sentence"].format(effect_abs=abs(eff), effect_pct=abs(eff) * 100, direction=direction)
    return f"Across {n_total} {cohort_label.lower()}, {text}."


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


def discover(db_path=DB_PATH, cohorts: dict = None, today: date = None, shuffles=SHUFFLES) -> dict:
    """Run every hypothesis over every cohort that clears the floor, plus
    platform-wide. `cohorts` is {restaurant_id: category or None}."""
    latest = _features.latest_by_restaurant(db_path=db_path)
    cohorts = cohorts or {}
    groups = {"platform": list(latest.values())}
    for rid, row in latest.items():
        c = cohorts.get(rid)
        if c:
            groups.setdefault(c, []).append(row)
    candidates = []
    for cohort, rows in groups.items():
        if not privacy.cohort_ok(len(rows)):
            continue
        for h in HYPOTHESES:
            cand = test_hypothesis(rows, h, shuffles=shuffles)
            if cand:
                cand["cohort"] = cohort
                cand["n_total"] = len(rows)
                cand["hypothesis"] = h
                candidates.append(cand)
    qs = benjamini_hochberg([c["p_value"] for c in candidates])
    for c, q in zip(candidates, qs):
        c["q_value"] = q
    active_keys = set()
    written = retired = 0
    conn = get_conn(db_path)
    try:
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
            label = (f"{categories.label(c['cohort']).lower()} on Cavnar" if c["cohort"] != "platform"
                     else "restaurants on Cavnar (all types)")
            row = {"key": key, "cohort": c["cohort"], "hypothesis": h["key"], "n_with": c["n_with"], "n_without": c["n_without"],
                   "effect": privacy.round_effect(c["effect"], 3), "effect_unit": h["unit"],
                   "cohen_d": privacy.round_effect(c["cohen_d"], 3), "p_value": round(c["p_value"], 4),
                   "q_value": round(c["q_value"], 4), "confidence": _confidence(c),
                   "sentence": _sentence(h, c, label, c["n_total"]),
                   "evidence": {"n": c["n_total"], "mean_with": privacy.round_effect(c["mean_with"], 3),
                                "mean_without": privacy.round_effect(c["mean_without"], 3), "behaviour": list(h["behaviour"]),
                                "outcome": h["outcome"], "rec_kinds": list(h["rec_kinds"]),
                                "pooled_types": c["cohort"] == "platform"}}
            privacy.assert_anonymous(row)
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
        tested = {c["cohort"] for c in candidates} | {c for c, rows in groups.items() if privacy.cohort_ok(len(rows))}
        for r in conn.execute("SELECT key, cohort FROM intel_patterns WHERE status='active'").fetchall():
            below_floor = not privacy.cohort_ok(len(groups.get(r["cohort"]) or []))
            if below_floor or (r["cohort"] in tested and r["key"] not in active_keys):
                conn.execute("UPDATE intel_patterns SET status='retired', computed_at=datetime('now') WHERE key=?", (r["key"],))
                retired += 1
        conn.commit()
    finally:
        conn.close()
    return {"cohorts_tested": sorted(c for c, rows in groups.items() if privacy.cohort_ok(len(rows))),
            "candidates": len(candidates), "active": written, "retired": retired}


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


def active(cohort: str = None, db_path=DB_PATH, include_platform=True, limit=20) -> list:
    """Active patterns for a cohort, with platform-wide ones after them,
    each re-confirmed within MAX_PATTERN_AGE_DAYS and carrying `as_of`
    (M/D/YY). Rows are anonymous by construction; asserted again on the
    way out."""
    fresh = f"AND last_confirmed >= datetime('now', '-{int(MAX_PATTERN_AGE_DAYS)} days')"
    conn = get_conn(db_path)
    try:
        if cohort and include_platform:
            rows = conn.execute(f"SELECT * FROM intel_patterns WHERE status='active' {fresh} AND cohort IN (?, 'platform') "
                                "ORDER BY CASE WHEN cohort=? THEN 0 ELSE 1 END, confidence DESC LIMIT ?",
                                (cohort, cohort, int(limit))).fetchall()
        elif cohort:
            rows = conn.execute(f"SELECT * FROM intel_patterns WHERE status='active' {fresh} AND cohort=? "
                                "ORDER BY confidence DESC LIMIT ?", (cohort, int(limit))).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM intel_patterns WHERE status='active' {fresh} "
                                "ORDER BY confidence DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
        d.pop("id", None)
        d["as_of"] = _as_of(d.get("last_confirmed"))
        out.append(privacy.assert_anonymous(strength_fields(d)))
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


def support_for(rec_kind: str, cohort: str = None, db_path=DB_PATH) -> dict | None:
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
