"""Restaurant DNA, told (intelligence/dna_story.py, owner 10/8/26): the
redesign's traits, shape, connections and still-learning list. The rule it
keeps from the 9/24/26 audit: no name without its measurement — a trait is
earned only by a measured figure against the rule printed with it, an axis
with nothing measured is None (never 0), and the read never carries dollars
or an unmeasured claim."""
import pytest

import models
from intelligence import dna, dna_story


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    for mod in (models, dna, dna_story):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)


def _profile(values, families=None):
    """A profile shaped as dna.profile returns it, from {dim: raw or None};
    every other dimension unmeasured. `families` limits it (a login's view)."""
    fams = []
    for fam, label in dna.FAMILIES:
        if families is not None and fam not in families:
            continue
        items = []
        for k, meta in dna.DIMENSIONS.items():
            if meta["family"] != fam:
                continue
            raw = values.get(k)
            items.append({"key": k, "label": meta["label"], "value": raw, "display": dna.display(k, raw),
                          "measured": raw is not None, "n": 10 if raw is not None else values.get("_n", {}).get(k, 0),
                          "needs": None if raw is not None else meta["needs"], "dormant": not meta["buildable"],
                          "history": [], "trend": None, "basis": "test"})
        fams.append({"key": fam, "label": label, "dimensions": items})
    measured = sum(1 for f in fams for x in f["dimensions"] if x["measured"])
    return {"available": True, "as_of": "10/8/26", "measured": measured, "of": 39, "coverage_pct": 50,
            "families": fams}


EJS = {"weekend_share": 0.61, "open_hours": 92.0, "overtime_intensity": 0.02, "labor_swing": 9.3, "labor_flex": 0.45,
       "labor_pct": 28.9, "rating_level": 4.42, "negative_share": 0.21, "reply_rate": 0.74, "reply_within_day": 0.21,
       "comp_rate": 0.9, "void_rate": 0.7, "rec_uptake": 1.0, "follow_through": 0.14, "sales_volatility": 0.22,
       "concept": "sports_bar", "service_type": "service:full_service"}


def test_traits_are_earned_by_a_measured_figure_against_a_stated_rule(db_path):
    s = dna_story.story(1, _profile(EJS), name="Simple EJ's", db_path=db_path)
    got = {t["key"]: t for t in s["traits"]}
    assert set(got) == {"weekend_driven", "long_hours", "overtime_controlled", "uneven_labor_days", "well_rated",
                        "answers_reviews", "tight_register", "open_to_advice"}
    assert got["weekend_driven"]["figure"] == "61%" and got["weekend_driven"]["rule"]
    assert got["uneven_labor_days"]["tone"] == "watch"
    # 28.9% labor is not "Labor Efficient" (28% or less), 22% swing is neither steady nor swingy
    assert "labor_efficient" not in got and "steady_demand" not in got and "swingy_demand" not in got
    for t in s["traits"]:
        assert t["sentence"] and t["rule"] and t["dims"] and t["figure"]


def test_an_unmeasured_dimension_never_names_a_trait(db_path):
    s = dna_story.story(1, _profile({"weekend_share": None, "open_hours": None}), db_path=db_path)
    assert s["traits"] == []
    keys = {l["key"] for l in s["learning"]}
    assert {"daypart", "predictability", "trend", "seasonality"} <= keys


def test_the_shape_is_50_at_the_stated_benchmark_and_none_when_unmeasured(db_path):
    anchors = {"sales_volatility": dna.DIMENSIONS["sales_volatility"]["anchor"][0]}
    s = dna_story.story(1, _profile(anchors), db_path=db_path)
    ax = {a["key"]: a for a in s["axes"]}
    assert ax["sales_stability"]["score"] == 50
    assert ax["predictability"]["score"] is None and ax["food_cost"]["score"] is None   # learning, never 0
    s2 = dna_story.story(1, _profile({"sales_volatility": 0.05}), db_path=db_path)
    assert {a["key"]: a for a in s2["axes"]}["sales_stability"]["score"] > 50          # steadier reads stronger


def test_a_login_without_labor_view_sees_no_labor_trait_axis_or_chain(db_path):
    p = _profile(EJS, families={"sales", "guests", "food", "marketing", "loop"})
    s = dna_story.story(1, p, db_path=db_path)
    assert not [t for t in s["traits"] if t["family"] == "labor"]
    assert not [a for a in s["axes"] if a["key"] in ("labor_efficiency", "labor_rhythm", "team_reliability")]
    assert "labor_chain" not in {c["key"] for c in s["connections"]}
    assert "overtime" not in s["headline"] and "labor" not in s["headline"].lower()


def test_the_read_says_only_what_was_measured_and_never_dollars(db_path):
    s = dna_story.story(1, _profile(EJS), name="Simple EJ's", db_path=db_path)
    h = s["headline"]
    assert h.startswith("Simple EJ's is a full-service sports bar open 92 hours a week that does 61%")
    assert "$" not in h and "Watch:" in h
    for c in s["connections"]:
        assert "$" not in c["sentence"]
    chain = {c["key"]: c for c in s["connections"]}["labor_chain"]
    assert "hours rise about 4%" in chain["sentence"] or "hours rise about 5%" in chain["sentence"]


def test_learning_progress_is_the_dimensions_own_count_capped_at_its_floor(db_path):
    p = _profile({"_n": {"wait_service_complaints": 19, "growth": 40, "daypart_mix": 11}})
    s = dna_story.story(1, p, db_path=db_path)
    got = {l["key"]: l for l in s["learning"]}
    assert got["service_complaints"]["progress"] == {"have": 19, "need": 20, "unit": "analysed reviews in 90 days",
                                                     "pct": 95}
    assert got["trend"]["progress"]["have"] == 13                                    # capped at the floor
    pcts = [l["progress"]["pct"] for l in s["learning"] if l.get("progress")]
    assert pcts == sorted(pcts, reverse=True)                                       # closest first
    assert got["beverage"]["dormant"] is True and "progress" not in got["beverage"]
    assert "guest_loyalty" in got                                                    # no dimension yet: says what would measure it


def test_no_story_before_the_profile_is_built(db_path):
    assert dna_story.story(1, {"available": False, "families": []}, db_path=db_path) == {}
