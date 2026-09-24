"""The engine's kind-level model: how much a KIND of recommendation is
supported at this restaurant, from factors that were measured.

score = Σ weight × factor over the factors that could be measured, with
the weights renormalised over those present, times coverage (the share of
the weights that could be measured). A factor with no data is absent, never
0.5 — an unmeasured thing does not vote. Deterministic: the same rows
produce the same score, so a test can pin it and a reader can see why in
`factors`.

It is NOT what an owner sees. Every owner-facing recommendation carries the
Recommendation Confidence object (contract K1) built by rec_trust.assess
from three measured dimensions (confidence_engine); this model is read by
the admin intelligence dashboard. `measurability` (formerly mislabelled
"historical accuracy") is how often this restaurant's forecasts and
results could be READ clearly — it says nothing about whether they were
right, and it feeds nothing called accuracy (CA2 #12).
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
from . import privacy, categories, scoring, patterns
from . import features as _features
from .stats import shrink

# Sum to 1.0 (a test holds it; INTELLIGENCE_ENGINE.md lists the same).
WEIGHTS = {
    "restaurant_history": 0.25,
    "platform_evidence": 0.20,
    "type_match": 0.10,
    "measurability": 0.10,
    "data_completeness": 0.15,
    "pattern_support": 0.10,
    "recent_changes": 0.10,     # the factor is (1 - change score): calm reads 1, churn reads 0
}
HIGH, MEDIUM = 0.70, 0.45


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports —
    re-audit B23)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _recent_changes(restaurant_id, days=14, db_path=DB_PATH) -> tuple[float, str]:
    """0 (calm) .. 1 (a lot changed): schedule edits, price changes, a new
    POS or connection in the window. Each is one signal; three saturate."""
    floor = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        edits = conn.execute("SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=? AND edited_at >= ?",
                             (restaurant_id, floor)).fetchone()[0] if _features._has_col(conn, "schedule_history", "edited_at") else 0
        reprices = conn.execute("SELECT COUNT(*) FROM recommendation_outcomes WHERE restaurant_id=? AND source='reprice' AND started_on >= ?",
                                (restaurant_id, floor)).fetchone()[0]
        pos = conn.execute("SELECT COUNT(*) FROM activity_log WHERE restaurant_id=? AND event_type IN ('pos_connected','pos_disconnected') "
                           "AND created_at >= ?", (restaurant_id, floor)).fetchone()[0]
    finally:
        conn.close()
    signals = (1 if edits else 0) + (1 if reprices else 0) + (1 if pos else 0)
    notes = [n for n, v in (("schedule edited", edits), ("prices changed", reprices), ("POS connection changed", pos)) if v]
    return min(1.0, signals / 3.0), ", ".join(notes) or "no operational changes in the last two weeks"


def _measurability(restaurant_id, db_path=DB_PATH):
    """How READABLE this restaurant's record is: 1 - mean |forecast error|
    (capped), blended with the share of evaluated outcomes that gave a clear
    verdict — a worsened result is as clear as an improved one, so this is
    never a measure of being right (probe: five worsened results score 1.0;
    CA2 #12). Named `_historical_accuracy` until the confidence audit.
    None when nothing scored."""
    conn = get_conn(db_path)
    try:
        errs = [abs(float(r[0])) for r in conn.execute(
            "SELECT error_pct FROM forecast_log WHERE restaurant_id=? AND error_pct IS NOT NULL ORDER BY scored_at DESC LIMIT 8",
            (restaurant_id,)).fetchall() if r[0] is not None]
        ev = conn.execute("SELECT verdict FROM recommendation_outcomes WHERE restaurant_id=? AND status='evaluated'",
                          (restaurant_id,)).fetchall()
    finally:
        conn.close()
    parts = []
    if errs:
        parts.append(max(0.0, 1.0 - min(1.0, sum(errs) / len(errs) / 50.0)))     # 50% error → 0
    if ev:
        clear = sum(1 for r in ev if r["verdict"] in ("improved", "worsened", "no_clear_change"))
        parts.append(clear / len(ev))
    if not parts:
        return None, "no scored forecasts or evaluated outcomes yet"
    return round(sum(parts) / len(parts), 3), f"{len(errs)} scored forecasts, {len(ev)} evaluated outcomes"


def score(restaurant_id: int, rec_kind: str, metric: str = None, cohort: str = None, restaurant=None,
          db_path: str = DB_PATH) -> dict:
    """{score, band, factors:[{name, value, weight, note}], caution}.

    `metric` is accepted for the facade's signature and not read: every
    factor here is per recommendation KIND, not per metric."""
    if restaurant is None:
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id, db_path=db_path)
    if cohort is None and restaurant is not None:
        cohort, _src = categories.category_for(restaurant)
    factors = []

    own = scoring.kind_stats(rec_kind, restaurant_id=restaurant_id, db_path=db_path)
    if own["measured"] or own["answered"]:
        # measured success first; acceptance stands in until something is measured
        if own["measured"]:
            v = shrink(own["success_rate"], own["measured"])
            note = f"this restaurant: {own['improved']} of {own['measured']} measured improved"
        else:
            v = shrink(own["acceptance_rate"], own["answered"], prior=0.5)
            note = f"this restaurant: {own['accepted']} of {own['answered']} accepted, none measured yet"
        # Structured, so a reader asks `measured` rather than sniffing the note.
        factors.append({"name": "restaurant_history", "value": round(v, 3), "note": note,
                        "measured": int(own["measured"] or 0), "improved": int(own.get("improved") or 0)})

    # The cohort (else the platform) WITHOUT this restaurant — its own
    # record is the restaurant_history factor above — and a success figure
    # only over MIN_COHORT restaurants that measured one: restaurants that
    # merely let a card expire are no floor for what one other restaurant
    # measured (re-audit B3).
    plat = (scoring.kind_stats(rec_kind, cohort=cohort, db_path=db_path, exclude_restaurant_id=restaurant_id)
            if cohort else None)
    scope = "cohort"
    if not plat or not plat.get("success_available"):
        plat = scoring.kind_stats(rec_kind, db_path=db_path, exclude_restaurant_id=restaurant_id)
        scope = "platform"
    if plat.get("success_available") and plat["measured"] and privacy.cohort_ok(plat.get("measured_restaurants")):
        # A cross-restaurant figure: over MIN_COHORT measuring restaurants,
        # counts only, and asserted anonymous before a card may carry it.
        factors.append(privacy.assert_anonymous({
            "name": "platform_evidence", "value": round(shrink(plat["success_rate"], plat["measured"]), 3),
            "note": (f"{plat['improved']} of {plat['measured']} measured across {plat['measured_restaurants']} "
                     "restaurants improved"),
            "scope": scope, "measured": plat["measured"], "restaurants": plat["measured_restaurants"],
            # Named from the cohort actually used (NS4 H4), never "like yours".
            "cohort_label": (f"{categories.label(cohort).lower()} on Cavnar" if scope == "cohort"
                             else "restaurants on Cavnar (all types)")}))

    if cohort:
        conn = get_conn(db_path)
        try:
            n = conn.execute("SELECT n FROM intel_benchmarks WHERE cohort=? ORDER BY week DESC LIMIT 1", (cohort,)).fetchone()
        finally:
            conn.close()
        ok = bool(n and privacy.cohort_ok(n[0]))
        factors.append({"name": "type_match", "value": 1.0 if ok else 0.4,
                        "note": f"{categories.label(cohort)}: {'enough similar restaurants to compare' if ok else 'too few similar restaurants yet'}"})

    meas, meas_note = _measurability(restaurant_id, db_path=db_path)
    if meas is not None:
        factors.append({"name": "measurability", "value": meas, "note": meas_note})

    latest = _features.latest(restaurant_id, db_path=db_path)
    if latest:
        factors.append({"name": "data_completeness", "value": round(float(latest["completeness"]), 3),
                        "note": f"{int(round(latest['completeness'] * 100))}% of the measures this engine reads are on file"})

    sup = patterns.support_for(rec_kind, cohort=cohort, db_path=db_path)
    if sup:
        factors.append({"name": "pattern_support", "value": float(sup["confidence"]), "note": sup["sentence"]})

    change, change_note = _recent_changes(restaurant_id, db_path=db_path)
    factors.append({"name": "recent_changes", "value": round(1.0 - change, 3), "note": change_note})

    total_w = sum(WEIGHTS[f["name"]] for f in factors)
    all_w = sum(WEIGHTS.values())
    weighted_mean = sum(WEIGHTS[f["name"]] * f["value"] for f in factors) / total_w if total_w else 0.0
    # Coverage: how much of the model could be measured. An unmeasured factor
    # does not vote, but it does not vanish either — one calm factor alone
    # must not read as certainty. Score = weighted mean × coverage.
    coverage = round(total_w / all_w, 3) if all_w else 0.0
    for f in factors:
        f["weight"] = round(WEIGHTS[f["name"]] / total_w, 3) if total_w else 0
    s = round(weighted_mean * coverage, 3)
    band = "high" if s >= HIGH else ("medium" if s >= MEDIUM else "low")
    caution = None
    if band == "low":
        why = next((f["note"] for f in sorted(factors, key=lambda f: f["value"]) if f["value"] < 0.5), None)
        caution = "Low confidence — " + (why or "little of this restaurant's own record supports it yet") + ". Treat as a question to check, not a finding."
    elif band == "medium":
        caution = "Medium confidence — worth trying; a tracker measures what follows, before and after."
    if band == "low" and coverage < 0.5 and caution:
        caution = "Low confidence — too little of this restaurant's record is measured yet to judge it. Treat as a question to check, not a finding."
    return {"score": s, "band": band, "coverage": coverage, "factors": factors, "caution": caution,
            "rec_kind": rec_kind, "cohort": cohort}


# ── one confidence per card (superseded) ────────────────────────────────────
#
# card_confidence is no longer called by any surface: every owner-facing card
# carries rec_trust.assess's measured Recommendation Confidence (K1) since the
# confidence audit (9/24/26). Kept, band logic only — it no longer returns a
# stand-in `score` — because tests pin its one-step rule.
# Candidate for future cleanup after additional verification.
#
# score() rates a recommendation KIND at a restaurant. Home printed it under
# cards whose own evidence line already carried a confidence — "high
# confidence" in the evidence, "Low confidence … treat as a question" under
# it — two opposite verdicts on one card. A card's confidence comes from its
# OWN evidence (how many reviews, weeks, counts stand behind it); the kind's
# record here may only move that by one band, and only when this restaurant
# has actually MEASURED results for the kind, never on platform priors or
# coverage alone.

BANDS = ("low", "medium", "high")
_BAND_LABEL = {"low": "Low confidence", "medium": "Medium confidence", "high": "High confidence"}
# How far this restaurant's own measured record for a kind has to sit from
# even before it moves a card's band.
KIND_UP_AT, KIND_DOWN_AT = 0.75, 0.35
# The cross-restaurant prior moves a band only when this restaurant has no
# measured record of the kind yet, the figure stands on at least
# privacy.MIN_COHORT restaurants (score() already requires it) and on this
# many measured results across them.
PRIOR_MIN_MEASURED = 10


def card_confidence(evidence_band: str, evidence_reason: str, kind_score: dict = None) -> dict:
    """{band, label, reason, adjusted, basis} for one card.

    `evidence_band`/`evidence_reason` describe the card's own evidence;
    `kind_score` is score()'s output for its kind (or None). The band moves
    by at most one step:

      * by this restaurant's OWN measured record of the kind (the
        restaurant_history factor, when it rests on measured outcomes);
      * else, once the cohort floor is met, by what restaurants like this
        one measured (the platform_evidence factor — ROI audit #45: it was
        computed and then ignored). Counts only, never a name; the factor is
        asserted anonymous where score() builds it and again here.

    A kind with no measured record anywhere leaves the card's own evidence
    alone. `basis` says which record moved it: own | cohort | platform.

    An unknown band is LOW, never medium — the diagnoses' rule (CA6
    duplicate #2). "Held" is never said: a before/after result is not an
    outcomes `held` grade, so the wording is the count."""
    band = evidence_band if evidence_band in BANDS else "low"
    reason = (evidence_reason or "").strip().rstrip(".")
    adjusted, basis = None, None
    factors = (kind_score or {}).get("factors") or []
    own = next((f for f in factors
                if f.get("name") == "restaurant_history" and int(f.get("measured") or 0) > 0), None)
    i = BANDS.index(band)
    if own is not None:
        said = f"{int(own.get('improved') or 0)} of {int(own['measured'])} measured here improved"
        if own["value"] >= KIND_UP_AT and i < 2:
            band, adjusted, basis = BANDS[i + 1], "up", "own"
            reason += f"; {said}"
        elif own["value"] <= KIND_DOWN_AT and i > 0:
            band, adjusted, basis = BANDS[i - 1], "down", "own"
            reason += f"; {said}"
    else:
        prior = next((f for f in factors if f.get("name") == "platform_evidence"), None)
        if (prior is not None and privacy.cohort_ok(prior.get("restaurants"))
                and int(prior.get("measured") or 0) >= PRIOR_MIN_MEASURED):
            privacy.assert_anonymous(prior)
            who = prior.get("cohort_label") or ("restaurants on Cavnar (all types)" if prior.get("scope") != "cohort"
                                                else "restaurants of this type on Cavnar")
            said = f"{who}: {prior['note']}"
            if prior["value"] >= KIND_UP_AT and i < 2:
                band, adjusted, basis = BANDS[i + 1], "up", prior.get("scope") or "platform"
                reason += f"; {said}"
            elif prior["value"] <= KIND_DOWN_AT and i > 0:
                band, adjusted, basis = BANDS[i - 1], "down", prior.get("scope") or "platform"
                reason += f"; {said}"
    return {"band": band, "label": _BAND_LABEL[band], "reason": reason or None, "adjusted": adjusted, "basis": basis,
            "caution": (f"Low confidence — {reason}. Treat it as a question to check."
                        if band == "low" and reason else None)}
