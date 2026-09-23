"""The confidence model: how much a recommendation to this restaurant
should be trusted, from factors that were measured.

score = Σ weight × factor over the factors that could be measured, with
the weights renormalised over those present. A factor with no data is
absent, never 0.5 — an unmeasured thing does not vote. Deterministic: the
same rows produce the same score, so a test can pin it and a reader can
see why in `factors`.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH
from . import privacy, categories, scoring, patterns
from . import features as _features
from .stats import shrink

WEIGHTS = {
    "restaurant_history": 0.25,
    "platform_evidence": 0.20,
    "type_match": 0.10,
    "historical_accuracy": 0.15,
    "data_completeness": 0.15,
    "pattern_support": 0.10,
    "recent_changes": 0.10,     # applied as a penalty (1 - change score)
}
HIGH, MEDIUM = 0.70, 0.45


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


def _historical_accuracy(restaurant_id, db_path=DB_PATH):
    """1 - mean |forecast error| (capped), blended with how often this
    restaurant's evaluated outcomes read clearly. None when nothing scored."""
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
    """{score, band, factors:[{name, value, weight, note}], caution}."""
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
        factors.append({"name": "restaurant_history", "value": round(v, 3), "note": note})

    plat = scoring.kind_stats(rec_kind, cohort=cohort, db_path=db_path) if cohort else None
    if not plat or not plat["available"]:
        plat = scoring.kind_stats(rec_kind, db_path=db_path)
    if plat["available"] and plat["measured"]:
        factors.append({"name": "platform_evidence", "value": round(shrink(plat["success_rate"], plat["measured"]), 3),
                        "note": f"{plat['improved']} of {plat['measured']} measured across {plat['restaurants']} restaurants improved"})

    if cohort:
        conn = get_conn(db_path)
        try:
            n = conn.execute("SELECT n FROM intel_benchmarks WHERE cohort=? ORDER BY week DESC LIMIT 1", (cohort,)).fetchone()
        finally:
            conn.close()
        ok = bool(n and privacy.cohort_ok(n[0]))
        factors.append({"name": "type_match", "value": 1.0 if ok else 0.4,
                        "note": f"{categories.label(cohort)}: {'enough similar restaurants to compare' if ok else 'too few similar restaurants yet'}"})

    acc, acc_note = _historical_accuracy(restaurant_id, db_path=db_path)
    if acc is not None:
        factors.append({"name": "historical_accuracy", "value": acc, "note": acc_note})

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
        caution = "Moderate confidence — worth trying, and the tracker will say whether it held."
    if band == "low" and coverage < 0.5 and caution:
        caution = "Low confidence — too little of this restaurant's record is measured yet to judge it. Treat as a question to check, not a finding."
    return {"score": s, "band": band, "coverage": coverage, "factors": factors, "caution": caution,
            "rec_kind": rec_kind, "cohort": cohort}


# ── one confidence per card ─────────────────────────────────────────────────
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


def card_confidence(evidence_band: str, evidence_reason: str, kind_score: dict = None) -> dict:
    """{band, label, reason, adjusted} for one card.

    `evidence_band`/`evidence_reason` describe the card's own evidence;
    `kind_score` is score()'s output for its kind (or None). Only the
    restaurant_history factor adjusts, and only when it rests on measured
    outcomes — a kind with no measured record here leaves the card's own
    evidence alone."""
    band = evidence_band if evidence_band in BANDS else "medium"
    reason = (evidence_reason or "").strip().rstrip(".")
    adjusted = None
    own = next((f for f in (kind_score or {}).get("factors") or []
                if f.get("name") == "restaurant_history" and "measured" in str(f.get("note") or "")
                and "none measured" not in str(f.get("note") or "")), None)
    if own is not None:
        i = BANDS.index(band)
        if own["value"] >= KIND_UP_AT and i < 2:
            band, adjusted = BANDS[i + 1], "up"
            reason += f"; this kind of change has held here before ({own['note'].split(': ', 1)[-1]})"
        elif own["value"] <= KIND_DOWN_AT and i > 0:
            band, adjusted = BANDS[i - 1], "down"
            reason += f"; this kind of change has not held here before ({own['note'].split(': ', 1)[-1]})"
    return {"band": band, "label": _BAND_LABEL[band], "reason": reason or None, "adjusted": adjusted,
            # Kept for older clients, which render `caution` under a low card.
            "caution": (f"Low confidence — {reason}. Treat it as a question to check."
                        if band == "low" and reason else None),
            # Always a number: shipped iOS builds decode `score` as non-optional.
            "score": {"low": 0.3, "medium": 0.55, "high": 0.8}[band]}
