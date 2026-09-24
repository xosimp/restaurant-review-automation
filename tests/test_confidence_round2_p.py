"""Confidence round 2, group P — the confidence engine's semantics, with the
owner's decision (9/24/26, "support score"): the overall % is how well
SUPPORTED the advice is — evidence × track record × freshness, every part
measured — never a probability it works; Historical Accuracy is the lift
against doing nothing.

Each test replays a probe from the blind re-audit (scratchpad blind1..4:
B1 probe1/probe2, B2 p1/p3/p4/p6/p8, B3 p1/p2, B4's Ask and Shift Quality
probes) with its seed and tolerance, and each fails on the code before
group P."""
import inspect
import json
import math
import random
import types
from datetime import date, timedelta

import pytest

import confidence_engine as ce
import data_freshness
import models
import rec_learning
import rec_ledger
import rec_trust
from models import Restaurant, create_restaurant

FR100 = {"pct": 100, "basis": "fresh", "as_of_iso": "2026-09-23", "errors": []}
FULL = ce.evidence(n=8, kind="reviews", basis="8 reviews")


def _own(k, n, **kw):
    return dict({"source": "own", "measured": n, "improved": k}, **kw)


def _overall(acc_record, ev=FULL, fr=FR100):
    return ce.assemble(ev, ce.accuracy(acc_record), fr)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _rid(db, **kw):
    fields = dict(name="Support Co", owner_email="s@x.test", module_reviews=1, module_labor=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db)


# ══ 1. Historical Accuracy = P(beats doing nothing), lift shown ═════════════

def test_p1_accuracy_is_the_chance_the_kind_beats_doing_nothing_not_a_rate_shrunk_to_even():
    """B2 #2: shrunk toward 0.5, 0 of 5 read 28% and a kind's accuracy fell
    as history grew. Now it is P(taken rate > the do-nothing rate)."""
    zero = ce.accuracy(_own(0, 5))
    five = ce.accuracy(_own(5, 5))
    assert zero["pct"] < 50 < five["pct"]
    assert zero["basis"] == "improved 0 of 5 times vs about 5% by chance"
    assert five["lift"] == {"improved": 5, "n": 5, "rate": 1.0, "untaken_improved": 0, "untaken_n": 0,
                            "do_nothing_rate": 0.05, "source": "stated"}
    assert five["beats_label"].endswith("likely to beat doing nothing")
    # held away from certainty either way
    assert ce.ACCURACY_BOUNDS[0] <= ce.accuracy(_own(0, 60))["pct"] and ce.accuracy(_own(60, 60))["pct"] <= 99


def test_p1_the_lift_sentence_compares_against_the_untaken_record_when_there_is_one():
    rec = _own(4, 6, base_rate=round((1 + 0.05 * 5) / (6 + 5), 3), base_rate_source="untaken", base_rate_n=6,
               untaken={"measured": 6, "improved": 1})
    a = ce.accuracy(rec)
    assert a["basis"] == "improved 4 of 6 times vs 1 of 6 when not acted on"
    assert a["lift"]["untaken_n"] == 6 and a["lift"]["source"] == "untaken"
    # doing better than the untaken record is more support than matching it
    same = ce.accuracy(_own(1, 6, base_rate=rec["base_rate"], base_rate_source="untaken", base_rate_n=6,
                            untaken={"measured": 6, "improved": 1}))
    assert a["pct"] > same["pct"]


def test_p1_the_beta_integral_matches_a_monte_carlo():
    r = random.Random(7)
    for at, bt, ad, bd in ((2.25, 7.75, 1.0, 19.0), (0.25, 9.75, 1.0, 19.0), (5.25, 4.75, 0.25, 4.75)):
        n = 60000
        mc = sum(1 for _ in range(n) if r.betavariate(at, bt) > r.betavariate(ad, bd)) / n
        assert abs(ce.p_greater(at, bt, ad, bd) - mc) < 0.01
    assert abs(ce.p_greater(2, 3, 2, 3) - 0.5) < 1e-6


def test_p1_b2_p4_a_do_nothing_kind_reads_less_support_than_a_real_effect_kind():
    """B2 p4 replayed with the measured improved rates (post group Q: do
    nothing stable 3.6%, drift 9.8%, a real -1pt labor effect 13.6%) and a
    clearly working kind (35%): the expected support of each at n measured
    results. Before group P a drifting do-nothing kind outranked the real
    one 62% of the time at n=5, and every kind's figure FELL with history."""
    EV = ce.evidence(n=4, kind="weekdays")

    def expected(p, n):
        tot = 0.0
        for k in range(n + 1):
            pr = math.comb(n, k) * p ** k * (1 - p) ** (n - k)
            tot += pr * ce.assemble(EV, ce.accuracy(_own(k, n)), FR100)["pct"]
        return tot
    for n in (10, 20, 50):
        nothing, drift, real, strong = expected(0.036, n), expected(0.098, n), expected(0.136, n), expected(0.35, n)
        assert nothing <= drift + 0.5 <= real + 1.0 <= strong + 1.0, n
        assert nothing < ce.NO_TRACK_RECORD_CAP                   # never above having no record
        assert strong > (90 if n >= 20 else 80), (n, strong)     # a kind that clearly works reads high
    # the do-nothing kind no longer "improves" its figure by failing longer
    assert expected(0.036, 50) <= expected(0.036, 10) + 1


def _sim_mean(rng, do_nothing, taken, draws=400):
    """Mean support % of a simulated kind: its taken results improve at
    `taken`, and the restaurant measured what doing nothing gives (untaken
    results) at `do_nothing` — kind_record's own fields."""
    vals = []
    for _ in range(draws):
        n, nu = rng.choice((5, 8, 12, 20, 30)), rng.choice((5, 8, 12, 20))
        k = sum(1 for _ in range(n) if rng.random() < taken)
        ku = sum(1 for _ in range(nu) if rng.random() < do_nothing)
        rec = _own(k, n, base_rate=round((ku + 0.05 * 5) / (nu + 5), 3), base_rate_source="untaken",
                   base_rate_n=nu, untaken={"measured": nu, "improved": ku})
        vals.append(_overall(rec)["pct"])
    return sum(vals) / len(vals)


def test_p1_ordering_simulated_kinds_with_more_lift_read_more_support():
    """The owner's check, simulated (seeded): kinds whose TRUE LIFT over
    doing nothing is 0, small and large produce non-decreasing MEAN support %
    once records exist, within a 1-point tolerance. Lift 0 includes a
    drifting restaurant where doing nothing already "improves" 30% of the
    time — the old shrink-to-even read ranked that kind (70.6) above a real
    small lift (62.9); against the measured do-nothing rate it no longer
    does (66.8 < 72.9 < 91.9)."""
    rng = random.Random(20260924)
    lift0_drift = _sim_mean(rng, 0.30, 0.30)
    lift0_stable = _sim_mean(rng, 0.05, 0.05)
    small = _sim_mean(rng, 0.05, 0.15)
    large = _sim_mean(rng, 0.05, 0.40)
    assert max(lift0_drift, lift0_stable) <= small + 1.0, (lift0_drift, lift0_stable, small)
    assert small <= large + 1.0 and large - max(lift0_drift, lift0_stable) > 15, (small, large)
    assert max(lift0_drift, lift0_stable) < ce.NO_TRACK_RECORD_CAP


# ══ cohort: only the prior's centre, only downward ═════════════════════════

def test_p1_b2_p6_a_cohort_never_stands_in_for_the_restaurants_own_record():
    """B2 p6 / B1 H9: 4 own results all worsened, cohort 10 of 12 improved →
    74% accuracy, 86% high, no caution. Now: no figure, the no-record cap,
    and the caution; a favourable cohort can't raise an own record either."""
    rec = {"source": "cohort", "measured": 4, "improved": 0, "prior_measured": 12, "prior_improved": 10}
    a = ce.accuracy(rec)
    k1 = _overall(rec)
    assert a["pct"] is None and "10 of 12 improved" in a["basis"] and "like yours" not in a["basis"]
    assert k1["pct"] == ce.NO_TRACK_RECORD_CAP and k1["band"] == "medium" and "No track record" in k1["caution"]
    own = ce.accuracy(_own(0, 5))
    with_good_peers = ce.accuracy(_own(0, 5, prior_measured=12, prior_improved=10))
    assert with_good_peers["pct"] == own["pct"] and with_good_peers["prior"]["source"] == "do_nothing"
    # peers who saw this kind do worse than chance may only pull it DOWN
    with_bad_peers = ce.accuracy(_own(1, 5, prior_measured=40, prior_improved=0))
    assert with_bad_peers["pct"] < ce.accuracy(_own(1, 5))["pct"] and with_bad_peers["prior"]["source"] == "cohort"


def test_p1_b2_p8_a_cohort_record_is_not_day_one_high():
    """B2 p8: a review card with a 7-of-10 cohort read 86% high on day 1."""
    coh = ce.accuracy({"source": "cohort", "measured": 0, "prior_measured": 10, "prior_improved": 7})
    k1 = ce.assemble(ce.evidence(n=200, kind="reviews"), coh, FR100)
    assert k1["pct"] == ce.NO_TRACK_RECORD_CAP and k1["band"] == "medium"


def test_p1_kind_record_reads_the_cohort_prior_at_any_own_count():
    src = inspect.getsource(rec_learning.kind_record)
    assert "if not own:" in src and "own = out[\"measured\"] >= MIN_MEASURED_FOR_RATE" in src
    assert "privacy.assert_anonymous(s)" in src


# ══ 2. the overall % — caps in order, named, reasoned ══════════════════════

def test_p2_b2_p1_a_short_record_that_does_not_beat_doing_nothing_cannot_lift_the_no_record_cap():
    """B2 #4 / p1: with no record the card is capped at 70; 2 of 5 read 77
    (high), 0 of 10 read 55 (medium)."""
    none = _overall(None)
    two = _overall(_own(2, 5))
    assert none["pct"] == 70 and two["pct"] <= 70 and two["caps_applied"] == ["record_unproven"]
    assert "doesn't yet show it beats doing nothing" in two["reason"]
    for k, n in ((0, 5), (0, 10), (0, 20)):
        o = _overall(_own(k, n))
        assert o["pct"] <= ce.RECORD_AGAINST_CAP and o["band"] == "low" and o["caps_applied"] == ["record_against"]
        assert "leans against it" in o["caution"]
    # a record that clears doing nothing reads above the no-record cap
    assert _overall(_own(4, 5))["pct"] > 70 and _overall(_own(4, 5))["caps_applied"] == []


def test_p2_b3_p2_freshness_binds_after_the_record_cap_and_a_broken_source_is_never_healthy():
    """B3 p2: with no record, freshness 100/93/79/60/50 all read 70 with only
    the no-track-record caution; an erroring source sat exactly on 50."""
    ev = ce.evidence(n=28, kind="trading_days", coverage=1.0)
    got = [ce.assemble(ev, ce.accuracy(None), {"pct": f, "basis": f"fresh={f}", "errors": []})["pct"]
           for f in (100, 93, 79, 60, 50)]
    # Freshness weighs on the figure AFTER the no-record cap: fresh data
    # reads the cap, data days old reads below it (was 70 at every level).
    assert got[:3] == [70, 70, 70] and 70 > got[3] > got[4] > ce.STALE_CAP, got
    err = ce.freshness([{"key": "pos", "label": "POS", "pct": 45, "basis": "Toast sync failing",
                         "as_of_iso": "2026-09-23", "error": "401"}])
    k1 = ce.assemble(ev, ce.accuracy(None), err)
    # a broken POS never reads the same as a healthy one: 49, not 70
    assert k1["pct"] == ce.STALE_CAP and "stale" in k1["caps_applied"]
    assert "out of date" in k1["caution"] and "out of date" in k1["reason"]
    rec = ce.assemble(ev, ce.accuracy(_own(5, 5)), err)
    assert rec["pct"] == ce.STALE_CAP and rec["caps_applied"] == ["stale"]
    # a failing source carries a caution even where the cap doesn't bind
    low = ce.assemble(ce.evidence(n=1, kind="trading_days"), ce.accuracy(None),
                      ce.freshness([{"key": "labor", "pct": 90, "basis": "Shifts through 9/23/26",
                                     "as_of_iso": "2026-09-23"},
                                    {"key": "pos", "label": "POS", "pct": None, "basis": "x", "error": "401"}]))
    assert low["caps_applied"] == [] and "failing (POS)" in low["caution"]


def test_p2_b1_h1b_an_unmeasured_freshness_never_raises_the_overall():
    """B1 H1b: 8 reviews and a 5-of-5 record read 87% with freshness
    unmeasured but 77% with freshness at 60%."""
    rec = _own(5, 5)
    unmeasured = ce.assemble(FULL, ce.accuracy(rec), ce.freshness([]))
    sixty = ce.assemble(FULL, ce.accuracy(rec), {"pct": 60, "basis": "x", "errors": []})
    assert unmeasured["pct"] <= ce.FRESHNESS_UNMEASURED_CAP < sixty["pct"]
    assert unmeasured["caps_applied"] == ["freshness_unmeasured"]
    assert unmeasured["caution"] == "No connected source confirms this is current, so it can't read above 49%."


def test_the_undated_caution_says_what_the_card_rests_on():
    """The undated caution names what is true — what the card rests on, or
    which sources aren't connected — never "nothing dates the data", which
    read as broken (9/24/26)."""
    rec = _own(5, 5)
    taps = ce.assemble(FULL, ce.accuracy(rec), ce.freshness([], rests_on="link taps only"))
    assert taps["pct"] <= ce.FRESHNESS_UNMEASURED_CAP
    assert taps["caution"] == ("This is based on link taps only; no connected source confirms it's current, "
                               "so it can't read above 49%.")
    off = [{"key": "pos", "label": "POS", "pct": None, "state": "not_connected"},
           {"key": "marketing", "label": "Marketing", "pct": None, "state": "not_connected"}]
    two = ce.assemble(FULL, ce.accuracy(rec), ce.freshness(off))
    assert two["caution"] == "POS and Marketing aren't connected to confirm this is current, so it can't read above 49%."
    one = ce.assemble(FULL, ce.accuracy(rec), ce.freshness(off[:1]))
    assert one["caution"].startswith("POS isn't connected")
    for c in (taps, two, one):
        assert "Nothing dates" not in c["caution"] and c["caps_applied"] == ["freshness_unmeasured"]


def test_p2_the_k1_object_carries_its_meaning_thresholds_and_caps():
    k1 = _overall(_own(2, 5))
    assert k1["meaning"] == "How well supported this is — not the chance it works" == ce.MEANING
    assert k1["thresholds"] == {"high": ce.HIGH_AT, "medium": ce.MEDIUM_AT} == {"high": 75, "medium": 50}
    caps = k1["caps"]
    assert (caps["no_track_record"], caps["stale"], caps["stale_below"]) == (70, 49, 50)
    assert k1["caps_applied"] == ["record_unproven"] and k1["sample"] is False and k1["version"] == 2
    assert k1["dimensions"]["evidence"]["n_full"] == ce.N_FULL["reviews"]
    s = ce.assemble(ce.evidence(n=9, kind="reviews", sample=True), ce.accuracy(None), FR100)
    assert s["sample"] is True and s["dimensions"]["evidence"]["n_full"] == 8
    u = ce.unknown()
    assert u["meaning"] == ce.MEANING and u["caps_applied"] == [] and u["thresholds"]["high"] == 75
    # never "certain"
    assert _overall(_own(60, 60))["pct"] <= ce.MAX_OVERALL


def test_p2_b1_m5_the_line_names_the_cap_that_set_the_figure():
    """B1 M5: "70% confidence — a count of 3 drafts on file" — the reason was
    the weakest dimension while the 70 came from the no-record cap."""
    k1 = ce.assemble(ce.evidence(n=8, kind="reviews", basis="8 reviews on the theme"), ce.accuracy(None), FR100)
    assert k1["caps_applied"] == ["no_track_record"] and k1["reason"].startswith("no track record here yet")
    uncapped = ce.assemble(ce.evidence(n=2, kind="reviews", basis="2 reviews"), ce.accuracy(None), FR100)
    assert uncapped["caps_applied"] == [] and uncapped["reason"] == "2 reviews"


def test_p9_thresholds_and_rates_are_single_sourced():
    assert ce.BASE_RATE_STATED == rec_learning.BASE_RATE_STATED
    assert ce.SHRINK_K == rec_learning.SHRINK_K and ce.MIN_MEASURED == rec_learning.MIN_MEASURED_FOR_RATE
    assert ce.thresholds() == {"high": 75, "medium": 50}
    assert ce.caps()["no_track_record"] == ce.NO_TRACK_RECORD_CAP and ce.caps()["stale"] == ce.STALE_CAP


# ══ 3. facts carry no confidence ═══════════════════════════════════════════

def test_p3_b1_probe1_home_facts_carry_no_confidence(db, monkeypatch):
    """B1 probe1 / B4 H5: a setup nudge, an urgent review and drafts waiting
    each read "70% confidence" from a default "1 row counted"."""
    import home_brief
    import uuid
    rid = _rid(db)
    c = models.get_conn(db)
    for i in range(3):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, sentiment, urgency, response_status, processed, draft_response) VALUES "
                  "(?, 'google', ?, 'A', 1, 'bad', date('now','-1 days'), datetime('now'), 'negative', 'high', "
                  "'drafted', 1, 'Sorry')", (rid, uuid.uuid4().hex))
    c.commit(); c.close()
    home_brief.invalidate()
    user = {"id": 1, "restaurant_id": rid, "base_restaurant_id": rid, "username": "o", "role": "owner",
            "is_admin": 0, "email": "s@x.test"}
    p, _ = home_brief.build_home_brief(user, fresh=True)
    facts = [a for a in p["attention"] if a["key"] in home_brief.HOME_FACT_KEYS]
    assert facts and all(a["confidence"] is None for a in facts), [(a["key"], a["confidence"]) for a in facts]
    for r in p["recommendations"]:
        if r["key"] in home_brief.HOME_FACT_KEYS:
            assert r["confidence"] is None
    src = inspect.getsource(home_brief)
    assert 'ev or {"n": 1, "kind": "count"' not in src


def test_p3_the_one_thing_hero_carries_none_for_a_fact():
    import business_intelligence as bi
    assert bi.one_thing_confidence(1, {"key": "urgent_reviews", "fact": True, "modules": ["reviews"]}) is None
    src = inspect.getsource(bi.one_thing_candidates)
    assert "fact=True" in src and '"kind": "count", "basis": f"{n} unanswered' not in src


# ══ 4. evidence semantics ══════════════════════════════════════════════════

def test_p4_b1_probe2g_food_drivers_count_what_they_rest_on_not_one_row():
    """B1 probe2g / B4 M3: menu and sourcing drivers read 100% and 80% on
    n=1 "count"."""
    import food_cost_intelligence as fci
    menu = ce.evidence(**fci.driver_evidence({"kind": "menu", "recipe_lines": 6, "recipe_unreviewed_lines": 3}))
    assert menu["kind"] == "recipe_lines" and menu["n"] == 3 and menu["n_full"] == 6 and menu["pct"] == 50
    src = ce.evidence(**fci.driver_evidence({"kind": "sourcing", "n_suppliers": 2}))
    assert src["kind"] == "supplier_quotes" and src["pct"] == round(100 * 2 / 3 * 0.8)
    por = ce.evidence(**fci.driver_evidence({"kind": "portion", "recounts": 1}))
    assert por["kind"] == "recounts" and por["pct"] == 25
    assert ce.evidence(**fci.driver_evidence({"kind": "menu", "recipe_lines": 0}))["pct"] is None


def test_p4_b1_probe2a_the_food_diagnosis_reads_the_food_data_not_zero(monkeypatch):
    """B1 probe2a / H4: a food diagnosis with no verified cross-check read 0%
    while the CFO prose called it "medium"."""
    import waste_trend
    monkeypatch.setattr(waste_trend, "load_waste_history", lambda rid, limit=None, **k: ([{}] * 6, 0))
    dg = {"confidence": "medium", "model_confidence": "high", "operational_evidence": []}
    ev = rec_trust.food_diagnosis_input(1, dg)
    e = ce.evidence(**ev)
    assert ev["kind"] == "weeks" and ev["n"] == 6 and ev["model_band"] == "medium" and e["pct"] == 65
    # corroborating modules count once each, and raise it boundedly
    dg2 = dict(dg, confidence="high", operational_evidence=[{"module": "labor", "verified": True},
                                                           {"module": "labor", "verified": True},
                                                           {"module": "reviews", "verified": True}])
    assert rec_trust.verified_evidence_count(dg2) == 2
    e2 = ce.evidence(**rec_trust.food_diagnosis_input(1, dg2))
    assert e2["corroborating"] == 2 and e2["pct"] == 100 and "2 other modules agree" in e2["basis"]


def test_p4_r_handoff_diagnosis_evidence_reads_the_capped_band_never_the_raw_one():
    ev = rec_trust.diagnosis_evidence({"confidence": "low", "model_confidence": "high"}, 20, "reviews", "x")
    assert ev["model_band"] == "low"
    for path in ("business_intelligence.py", "home_brief.py"):
        s = open(path).read()
        assert 'rec_trust.diagnosis_evidence(dg, _n_ev' not in s and "diagnosis_evidence(_dg, len(" not in s


def test_p4_b2_12_a_daily_report_action_counts_nights_of_observation():
    """B2 #12 / B4 M3: three cited facts of one night scored a full sample."""
    from dsr import narrative
    F = types.SimpleNamespace(details={"sales.baselines": {"forecast": {"samples": 8}}}, metrics={})
    assert narrative.nights_behind(["sales.net", "labor.pct", "food.low_stock"], F) == 1
    assert narrative.nights_behind(["sales.vs_last_week_pct"], F) == 2
    assert narrative.nights_behind(["sales.vs_forecast_pct", "sales.vs_last_week"], F) == 9
    one = ce.evidence(n=1, kind="nights")
    full = ce.evidence(n=9, kind="nights")
    assert one["pct"] == round(100 / 8) and full["pct"] == 100
    assert 'kind": "nights"' in inspect.getsource(narrative.action_confidence)


def test_p6_b4_m2_agreement_raises_evidence_boundedly_and_never_makes_a_cause_high():
    base = ce.evidence(n=4, kind="reviews")["pct"]
    one = ce.evidence(n=4, kind="reviews", corroborating=1)["pct"]
    two = ce.evidence(n=4, kind="reviews", corroborating=2)["pct"]
    many = ce.evidence(n=4, kind="reviews", corroborating=9)["pct"]
    assert base < one < two == many == round(100 * 0.5 * 1.5)
    link = ce.evidence(n=3, kind="evidence_items", flags=("inferred",), corroborating=2)
    assert link["pct"] == ce.PARTIAL_CAP
    import business_intelligence as bi
    assert bi.link_evidence_input({"modules": ["labor", "reviews"], "evidence": ["a", "b"]})["corroborating"] == 1


# ══ 5. one confidence per key per build ════════════════════════════════════

def test_p5_b1_h3_one_key_one_figure_in_a_build(db):
    """B1 H3 / B4 M1: the same review diagnosis read 73% on the Home card and
    87% in the hero — two evidence inputs for one key."""
    rid = _rid(db)
    ctx = rec_trust.Context(rid, db_path=db)
    a = rec_trust.assess(rid, "diag_review:service", evidence={"n": 9, "kind": "reviews"},
                         sources=("reviews",), ctx=ctx)
    b = rec_trust.assess(rid, "diag_review:service", evidence={"n": 2, "kind": "reviews", "flags": ("sampled",)},
                         sources=("reviews",), ctx=ctx)
    assert a == b and a is not b
    # the Reviews tab, Home's card and the hero read ONE input for a diagnosis
    dg = {"category": "service", "mention_count": 9, "window_days": 90, "confidence": "medium",
          "operational_evidence": []}
    row = {"reviews_live": 1, "google_place_id": "ChIJx"}
    home = rec_trust.review_diagnosis_input(dg, row)
    assert home["n"] == 9 and home["flags"] == ("sampled",) and home["model_band"] == "medium"
    import home_brief
    import review_intelligence
    import business_intelligence as bi
    for mod in (home_brief, review_intelligence, bi):
        assert "review_diagnosis_input(" in inspect.getsource(mod)


def test_p5_the_web_hero_never_duplicates_a_card_with_the_same_key():
    html = open("templates/dashboard.html").read()
    assert "hbFocusKey" in html


# ══ 7. Shift Quality: one figure, a documented cap, every schedule source ══

def test_p7_b4_h3_the_shift_quality_score_is_a_cap_not_a_window_and_the_panel_agrees(db):
    """B4 H3 / B1 H2: at score 62 the item's Why? said "only 62% of the
    window measured"; at 85 the pill said High while items read 70%."""
    rid = _rid(db)
    for sc, reasons in ((62, ["3 of 9 scheduled staff have no Operational Score."]), (85, [])):
        q = {"confidence": {"score": sc, "reasons": reasons}}
        items = [{"text": "Fill the gap", "key": "schedule_coverage:fri"}]
        rec_trust.attach_schedule_confidence(rid, q, items, db_path=db)
        panel, item = q["confidence_detail"], items[0]["confidence"]
        assert panel["pct"] == item["pct"], sc
        basis = item["dimensions"]["evidence"]["basis"]
        assert "window" not in basis and f"{sc}% complete" in basis
        assert item["dimensions"]["evidence"]["pct"] == sc
    assert rec_trust.schedule_evidence({"confidence": {"score": 70}})["cap"] == 70.0
    assert 'sources=srcs' in inspect.getsource(rec_trust.attach_schedule_confidence)
    assert data_freshness.sources_for(["schedule"]) == ("labor", "pos", "sales", "weather")


# ══ 8. admin: an ORDER check, the learning rule, confidence at acceptance ══

def test_p8_the_ordering_check_flags_a_higher_band_doing_worse():
    bad = [(85, 1 if i < 5 else 0) for i in range(25)] + [(30, 1 if i < 20 else 0) for i in range(25)]
    o = ce.ordering(bad, 20)
    assert o["ordered"] is False and o["violations"][0]["higher"] == "75-100"
    good = [(85, 1 if i < 20 else 0) for i in range(25)] + [(30, 1 if i < 5 else 0) for i in range(25)]
    g = ce.ordering(good, 20)
    assert g["ordered"] is True and g["violations"] == [] and g["spearman"] > 0
    thin = ce.ordering([(85, 0)] * 3 + [(30, 1)] * 3, 20)
    assert thin["ordered"] is None                               # nothing judged under the floor


def _episode(rid, key, conf_pct, db, verdict=None, tracker=None, dollars=None):
    k1 = ce.assemble(ce.evidence(n=8, kind="reviews"), ce.accuracy(None),
                     ce.freshness([{"key": "reviews", "pct": 100, "as_of_iso": "2026-09-23"}]))
    k1["pct"] = conf_pct
    rec_ledger.present_many(rid, [{"key": key, "module": "reviews", "confidence": k1, "dollar_value": dollars}],
                            "home", db_path=db)
    rec_ledger.record(rid, key, "accepted", db_path=db)
    if tracker is not None:
        c = models.get_conn(db)
        cur = c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                        "status, verdict, started_on, evaluate_on, after_start, after_end, concurrent, "
                        "dollars_monthly) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, "home", key, "t", tracker.get("metric", f"m:{key}"), "evaluated", verdict,
                         "2026-08-01", "2026-08-29", "2026-08-01", "2026-08-29",
                         json.dumps(tracker.get("concurrent")) if tracker.get("concurrent") is not None else None,
                         tracker.get("dollars")))
        c.commit(); c.close()
        rec_ledger.link_tracker(rid, key, cur.lastrowid, db_path=db)
    elif verdict:
        rec_ledger.record(rid, key, "outcome", meta={"verdict": verdict}, db_path=db)


def test_p8_admin_calibration_counts_by_the_learning_rule_and_flags_disorder(db):
    """B2 #9 + Q handoff: the admin pairs could include confounded results,
    it scored the first-shown figure, and it read the % as a probability."""
    import admin_ops
    rid = _rid(db)
    # a confounded win (a trend already under way) is unknown, never a pair
    _episode(rid, "top_issue:conf", 80, db, verdict="improved",
             tracker={"concurrent": [{"kind": "pre_trend", "label": "Already moving this way"}]})
    out = admin_ops.confidence_calibration(days=30)
    assert out["n"] == 0
    # 25 high-support that mostly failed, 25 low-support that mostly worked
    for i in range(25):
        _episode(rid, f"top_issue:h{i}", 85, db, verdict="improved" if i < 5 else "worsened")
        _episode(rid, f"top_issue:l{i}", 30, db, verdict="improved" if i < 20 else "worsened")
    out = admin_ops.confidence_calibration(days=30)
    assert out["n"] == 50 and out["meaning"] == ce.MEANING and out["ordering"]["ordered"] is False
    assert any(a["where"] == "overall" and "Higher support is doing worse" in a["title"] for a in out["alerts"])
    assert out["versions"] == {str(ce.VERSION): 50} and "not a probability" in out["brier_note"]


def test_p8_admin_scores_the_confidence_the_owner_took_it_at(db):
    import admin_ops
    rid = _rid(db)
    key = "top_issue:accept"
    k1 = ce.assemble(ce.evidence(n=8, kind="reviews"), ce.accuracy(None),
                     ce.freshness([{"key": "reviews", "pct": 100, "as_of_iso": "2026-09-23"}]))
    rec_ledger.present_many(rid, [{"key": key, "module": "reviews", "confidence": dict(k1, pct=30)}], "home",
                            db_path=db)
    rec_ledger.present_many(rid, [{"key": key, "module": "reviews", "confidence": dict(k1, pct=90)}],
                            "brief_email", db_path=db)
    rec_ledger.record(rid, key, "accepted", db_path=db)
    rec_ledger.record(rid, key, "outcome", meta={"verdict": "improved"}, db_path=db)
    c = models.get_conn(db)
    eps = admin_ops._episodes(c, "2000-01-01 00:00:00", rid)
    c.close()
    scored = admin_ops._calibration_scored(eps)
    assert len(scored) == 1 and scored[0]["confidence_pct"] == 30          # the first showing is kept...
    assert scored[0]["score_confidence"] == 90 and scored[0]["scored_on"] == "acceptance"


def test_p8_dollar_calibration_counts_by_the_same_result_rule(db):
    import admin_ops
    rid = _rid(db)
    _episode(rid, "trim_day:Mon", 70, db, verdict="improved", dollars=500,
             tracker={"concurrent": [{"kind": "level_shift"}], "dollars": 400})
    out = admin_ops.recommendation_calibration(days=30)
    assert out["total"]["n"] == 0


def test_p8_the_admin_view_renders_the_ordering_check():
    html = open("templates/admin.html").read()
    fn = html[html.index("function confidenceCalibration"):]
    fn = fn[:fn.index("\n}\n")]
    assert "k.ordering" in fn and "k.alerts" in fn and "not the meaning of the %" in fn
    # rendered end to end under node: tests/test_confidence_web.py::test_admin_calibration_view_renders_k7


# ══ 10. leftovers ═════════════════════════════════════════════════════════

def test_p10_mobile_attention_carries_dollars_basis_and_owner_added_competitors_are_provisional():
    import mobile_api
    import competitor
    src = inspect.getsource(mobile_api)
    assert '"dollars_basis": a.get("dollars_basis")' in src
    csrc = inspect.getsource(competitor)
    block = csrc[csrc.index('"match_basis": "added by you"'):][:600]
    assert '"rating_is_provisional"' in block and "MIN_REVIEWS_FOR_A_MEANINGFUL_RATING" in block


# ══ 11. clients read the meaning and the lift ══════════════════════════════

def test_p11_the_web_why_panel_leads_with_the_meaning_and_shows_the_lift():
    html = open("templates/dashboard.html").read()
    blk = html[html.index("window.cavConf={"):]
    start = html.rindex("(function(){", 0, html.index("window.cavConf={"))
    js = html[start:html.index("window.cavConf={")]
    assert "n.meaning" in js and "beats_label" in js
    assert "pulled toward 50%" not in js
    assert blk
