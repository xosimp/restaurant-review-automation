"""The Operational Score capability layer, and shift strength.

Built from a customer's own question: "does the AI know how good each
employee is, and what if it schedules my two worst bartenders together on a
Saturday night?" Availability said yes. Nothing said they were the two
weakest.
"""
import json

import pytest

import labor
import models
from models import (CAPABILITY_ATTRIBUTES, CapabilityError, capability_coverage,
                    get_capabilities, get_operational_scores,
                    get_role_strength_thresholds, get_shift_leader_rules,
                    set_capability, validate_strength_thresholds)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) "
                 f"VALUES ({','.join('?' for _ in cols)})", tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _row(date, day, name, role, start="5:00pm"):
    return {"date": date, "day": day, "employee": name, "role": role, "shift_start": start}


# ── The capability layer, not a rating column ──────────────────────────────

def test_a_rating_is_stored_against_an_attribute_not_a_column(db_path):
    """Version 1 exposes one attribute. Adding closing ability or trainer
    status later must be a registry entry, not a migration."""
    _restaurant(db_path)
    set_capability(1, "Sarah", score=5, db_path=db_path)
    caps = get_capabilities(1, db_path=db_path)
    assert caps["Sarah"]["overall"]["score"] == 5


def test_a_second_attribute_needs_no_schema_change(db_path):
    """can_close is registered and not surfaced in V1. It has to already
    work, or the architecture claim is empty."""
    _restaurant(db_path)
    set_capability(1, "Sarah", score=5, db_path=db_path)
    set_capability(1, "Sarah", attribute="can_close", flag=True, db_path=db_path)
    caps = get_capabilities(1, db_path=db_path)
    assert caps["Sarah"]["overall"]["score"] == 5
    assert caps["Sarah"]["can_close"]["flag"] is True


def test_only_version_one_attributes_are_offered():
    v1 = [k for k, v in CAPABILITY_ATTRIBUTES.items() if v.get("v1")]
    assert v1 == ["overall"]
    assert "can_close" in CAPABILITY_ATTRIBUTES


def test_each_attribute_carries_its_own_provenance(db_path):
    _restaurant(db_path)
    set_capability(1, "Sarah", score=4, notes="Great on volume", updated_by="will", db_path=db_path)
    c = get_capabilities(1, db_path=db_path)["Sarah"]["overall"]
    assert c["updated_by"] == "will"
    assert c["notes"] == "Great on volume"
    assert c["updated_at"]


def test_a_rating_can_be_changed(db_path):
    _restaurant(db_path)
    set_capability(1, "Sarah", score=2, db_path=db_path)
    set_capability(1, "Sarah", score=5, db_path=db_path)
    assert get_operational_scores(1, db_path=db_path)["Sarah"] == 5


# ── Validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [0, 6, -1, 99])
def test_a_score_outside_the_scale_is_refused(db_path, bad):
    _restaurant(db_path)
    with pytest.raises(CapabilityError):
        set_capability(1, "Sarah", score=bad, db_path=db_path)


def test_a_score_that_is_not_a_number_is_refused(db_path):
    _restaurant(db_path)
    with pytest.raises(CapabilityError):
        set_capability(1, "Sarah", score="excellent", db_path=db_path)


def test_an_unknown_attribute_is_refused(db_path):
    _restaurant(db_path)
    with pytest.raises(CapabilityError):
        set_capability(1, "Sarah", attribute="vibes", score=5, db_path=db_path)


def test_an_empty_employee_name_is_refused(db_path):
    _restaurant(db_path)
    with pytest.raises(CapabilityError):
        set_capability(1, "   ", score=5, db_path=db_path)


def test_clearing_a_rating_is_different_from_rating_one(db_path):
    """"Not rated yet" and "rated 1" are different facts, and the scheduler
    treats them differently."""
    _restaurant(db_path)
    set_capability(1, "Sarah", score=3, db_path=db_path)
    set_capability(1, "Sarah", score=None, db_path=db_path)
    assert "Sarah" not in get_operational_scores(1, db_path=db_path)


# ── Migration: dormant until somebody is rated ─────────────────────────────

def test_an_existing_restaurant_starts_with_the_feature_dormant(db_path):
    """No backfill and no default rating. Scheduling works exactly as it did
    until the first rating is entered."""
    _restaurant(db_path)
    cov = capability_coverage(1, ["Sarah", "Jake"], db_path=db_path)
    assert cov["active"] is False
    assert cov["rated"] == 0 and cov["total"] == 2
    assert cov["unrated"] == ["Jake", "Sarah"]


def test_rating_one_person_activates_it(db_path):
    _restaurant(db_path)
    set_capability(1, "Sarah", score=5, db_path=db_path)
    cov = capability_coverage(1, ["Sarah", "Jake"], db_path=db_path)
    assert cov["active"] is True
    assert cov["rated"] == 1 and cov["unrated"] == ["Jake"]
    assert cov["pct"] == 50


# ── Shift strength ─────────────────────────────────────────────────────────

def test_two_fives_make_ten():
    out = labor.shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Jake", "Bartender")],
        {"Sarah": 5, "Jake": 5})
    b = out[("2026-09-19", "night", "Bartender")]
    assert b["strength"] == 10


def test_two_twos_make_four_and_are_not_the_same_schedule():
    out = labor.shift_strength(
        [_row("2026-09-19", "Saturday", "Pat", "Bartender"),
         _row("2026-09-19", "Saturday", "Sam", "Bartender")],
        {"Pat": 2, "Sam": 2})
    assert out[("2026-09-19", "night", "Bartender")]["strength"] == 4


def test_an_unrated_employee_contributes_nothing_and_is_named():
    """Scoring them a middle 3 would invent a fact about a person, and the
    owner would never learn the rating was missing."""
    out = labor.shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Newbie", "Bartender")],
        {"Sarah": 5})
    b = out[("2026-09-19", "night", "Bartender")]
    assert b["strength"] == 5
    assert b["unrated"] == ["Newbie"]


def test_a_double_shift_counts_one_person_once():
    out = labor.shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender", "11:00am"),
         _row("2026-09-19", "Saturday", "Sarah", "Bartender", "11:30am")],
        {"Sarah": 5})
    assert out[("2026-09-19", "morning", "Bartender")]["strength"] == 5


def test_morning_and_night_are_separate_shifts():
    out = labor.shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender", "10:00am"),
         _row("2026-09-19", "Saturday", "Jake", "Bartender", "6:00pm")],
        {"Sarah": 5, "Jake": 4})
    assert out[("2026-09-19", "morning", "Bartender")]["strength"] == 5
    assert out[("2026-09-19", "night", "Bartender")]["strength"] == 4


def test_roles_are_scored_separately():
    out = labor.shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Cook1", "Cook")],
        {"Sarah": 5, "Cook1": 3})
    assert out[("2026-09-19", "night", "Bartender")]["strength"] == 5
    assert out[("2026-09-19", "night", "Cook")]["strength"] == 3


# ── The question that started this ─────────────────────────────────────────

def test_the_two_weakest_bartenders_on_a_saturday_are_caught():
    """Erik's exact scenario. Availability said yes; nothing said they were
    his two weakest."""
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Pat", "Bartender"),
         _row("2026-09-19", "Saturday", "Sam", "Bartender")],
        {"Pat": 2, "Sam": 2, "Sarah": 5, "Jake": 5},
        {"Bartender": 10})
    assert len(out["shortfalls"]) == 1
    sf = out["shortfalls"][0]
    assert sf["strength"] == 4 and sf["target"] == 10
    assert sf["short_by"] == 6
    assert "Pat (2)" in sf["reason"] and "Sam (2)" in sf["reason"]


def test_two_strong_bartenders_satisfy_the_target():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Jake", "Bartender")],
        {"Sarah": 5, "Jake": 5}, {"Bartender": 10})
    assert out["shortfalls"] == []
    assert len(out["met"]) == 1
    assert out["met"][0]["strength"] == 10


def test_a_role_with_no_threshold_is_not_judged():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Pat", "Host")],
        {"Pat": 1}, {"Bartender": 10})
    assert out["shortfalls"] == [] and out["met"] == []


def test_different_roles_have_different_thresholds():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Jake", "Bartender"),
         _row("2026-09-19", "Saturday", "C1", "Cook"),
         _row("2026-09-19", "Saturday", "C2", "Cook"),
         _row("2026-09-19", "Saturday", "C3", "Cook")],
        {"Sarah": 5, "Jake": 5, "C1": 3, "C2": 4, "C3": 3},
        {"Bartender": 10, "Cook": 15})
    by_role = {s["role"]: s for s in out["shortfalls"]}
    assert "Bartender" not in by_role
    assert by_role["Cook"]["strength"] == 10 and by_role["Cook"]["short_by"] == 5


def test_the_shortfall_explains_a_missing_rating_specifically():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Newbie", "Bartender")],
        {"Sarah": 5}, {"Bartender": 10})
    reason = out["shortfalls"][0]["reason"]
    assert "Newbie" in reason and "no Operational Score" in reason


def test_only_one_person_available_says_so():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender")],
        {"Sarah": 5}, {"Bartender": 10})
    assert "Only Sarah (5) was available" in out["shortfalls"][0]["reason"]


def test_nothing_is_checked_when_nobody_is_rated():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Pat", "Bartender")], {}, {})
    assert out["checked"] is False
    assert out["shortfalls"] == []


# ── Shift leader requirements ──────────────────────────────────────────────

def _sat_rule(**kw):
    base = {"days": ["Saturday"], "daypart": "night", "role": "Bartender",
            "min_score": 5, "count": 1}
    base.update(kw)
    return base


def test_saturday_dinner_needs_a_five_rated_bartender():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Pat", "Bartender"),
         _row("2026-09-19", "Saturday", "Sam", "Bartender")],
        {"Pat": 3, "Sam": 3}, {}, leader_rules=[_sat_rule()])
    assert len(out["leader_misses"]) == 1
    m = out["leader_misses"][0]
    assert m["found"] == 0
    assert "scoring 5 or above" in m["reason"]


def test_a_five_rated_bartender_satisfies_the_rule():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Pat", "Bartender")],
        {"Sarah": 5, "Pat": 3}, {}, leader_rules=[_sat_rule()])
    assert out["leader_misses"] == []


def test_a_friday_kitchen_rule_needs_a_four_or_better_cook():
    rule = {"days": ["Friday"], "daypart": "night", "role": "Cook",
            "min_score": 4, "count": 1}
    weak = labor.verify_shift_strength(
        [_row("2026-09-18", "Friday", "C1", "Cook"),
         _row("2026-09-18", "Friday", "C2", "Cook")],
        {"C1": 3, "C2": 3}, {}, leader_rules=[rule])
    assert len(weak["leader_misses"]) == 1
    ok = labor.verify_shift_strength(
        [_row("2026-09-18", "Friday", "C1", "Cook"),
         _row("2026-09-18", "Friday", "C2", "Cook")],
        {"C1": 4, "C2": 3}, {}, leader_rules=[rule])
    assert ok["leader_misses"] == []


def test_a_rule_only_applies_to_the_days_it_names():
    out = labor.verify_shift_strength(
        [_row("2026-09-16", "Wednesday", "Pat", "Bartender")],
        {"Pat": 1}, {}, leader_rules=[_sat_rule()])
    assert out["leader_misses"] == []


def test_a_rule_only_applies_to_the_daypart_it_names():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Pat", "Bartender", "9:00am")],
        {"Pat": 1}, {}, leader_rules=[_sat_rule()])
    assert out["leader_misses"] == []


def test_a_rule_can_require_more_than_one_qualified_person():
    out = labor.verify_shift_strength(
        [_row("2026-09-19", "Saturday", "Sarah", "Bartender"),
         _row("2026-09-19", "Saturday", "Pat", "Bartender")],
        {"Sarah": 5, "Pat": 3}, {}, leader_rules=[_sat_rule(count=2)])
    assert out["leader_misses"][0]["found"] == 1


# ── Thresholds ─────────────────────────────────────────────────────────────

def test_thresholds_are_configurable_not_hardcoded(db_path):
    _restaurant(db_path, role_strength_json=json.dumps({"Bartender": 10, "Cook": 15}))
    assert get_role_strength_thresholds(1, db_path=db_path) == {"Bartender": 10.0, "Cook": 15.0}


def test_an_unconfigured_restaurant_has_no_thresholds(db_path):
    _restaurant(db_path)
    assert get_role_strength_thresholds(1, db_path=db_path) == {}


def test_a_negative_threshold_is_rejected_by_validation():
    assert validate_strength_thresholds({"Bartender": -5})


def test_an_unreachable_threshold_is_called_out():
    """A target no roster could ever meet is a warning the owner sees every
    week and learns to ignore."""
    problems = validate_strength_thresholds(
        {"Bartender": 30}, {"A": 5, "B": 5}, {"A": "Bartender", "B": "Bartender"})
    assert problems and "can never be met" in problems[0]


def test_a_reachable_threshold_raises_nothing():
    assert validate_strength_thresholds(
        {"Bartender": 10}, {"A": 5, "B": 5}, {"A": "Bartender", "B": "Bartender"}) == []


def test_corrupt_threshold_json_does_not_break_scheduling(db_path):
    _restaurant(db_path, role_strength_json="{not json")
    assert get_role_strength_thresholds(1, db_path=db_path) == {}


def test_corrupt_leader_rules_do_not_break_scheduling(db_path):
    _restaurant(db_path, shift_leader_rules_json="[[[")
    assert get_shift_leader_rules(1, db_path=db_path) == []


def test_a_rule_without_a_role_is_ignored(db_path):
    _restaurant(db_path, shift_leader_rules_json=json.dumps([{"days": ["Saturday"]}]))
    assert get_shift_leader_rules(1, db_path=db_path) == []


# ── Notes outlive a rating tap ─────────────────────────────────────────────

def test_changing_a_score_does_not_erase_the_note(db_path):
    """The rating control sends a score on every tap and never carries the
    note with it. Writing the note unconditionally erased whatever the
    owner had written the next time they nudged that person's score."""
    _restaurant(db_path)
    set_capability(1, "Sarah", score=3, notes="Great on brunch, shaky at close",
                   db_path=db_path)
    set_capability(1, "Sarah", score=4, db_path=db_path)
    caps = get_capabilities(1, db_path=db_path)
    assert caps["Sarah"]["overall"]["score"] == 4
    assert caps["Sarah"]["overall"]["notes"] == "Great on brunch, shaky at close"


def test_an_empty_note_clears_one_deliberately(db_path):
    """Not passing notes means keep. Passing an empty string means clear —
    otherwise there would be no way to take a note back."""
    _restaurant(db_path)
    set_capability(1, "Sarah", score=3, notes="Out of date", db_path=db_path)
    set_capability(1, "Sarah", score=3, notes="", db_path=db_path)
    assert get_capabilities(1, db_path=db_path)["Sarah"]["overall"]["notes"] is None


def test_un_rating_somebody_keeps_what_was_written_about_them(db_path):
    """A note explaining why a rating sat where it did outlives the rating.
    The person still reads as unrated to everything downstream."""
    _restaurant(db_path)
    set_capability(1, "Sarah", score=2, notes="New, still learning", db_path=db_path)
    set_capability(1, "Sarah", score=None, db_path=db_path)
    caps = get_capabilities(1, db_path=db_path)
    assert caps["Sarah"]["overall"]["score"] is None
    assert caps["Sarah"]["overall"]["notes"] == "New, still learning"
    assert "Sarah" not in get_operational_scores(1, db_path=db_path)
    assert capability_coverage(1, ["Sarah"], db_path=db_path)["rated"] == 0


def test_un_rating_somebody_with_no_note_leaves_no_row(db_path):
    _restaurant(db_path)
    set_capability(1, "Sarah", score=2, db_path=db_path)
    set_capability(1, "Sarah", score=None, db_path=db_path)
    assert get_capabilities(1, db_path=db_path) == {}


# ── One role, however it happens to be spelled ─────────────────────────────

def test_a_target_applies_whatever_case_the_schedule_uses():
    """The role an owner types into the targets editor ("Bartender") and
    the one the generated CSV carries ("bartender") are one job. Matching
    exactly meant every target silently checked nothing."""
    rows = [_row("2026-09-12", "Saturday", "Pat", "bartender"),
            _row("2026-09-12", "Saturday", "Sam", "bartender")]
    out = labor.verify_shift_strength(rows, {"Pat": 3, "Sam": 3}, {"Bartender": 10})
    assert len(out["shortfalls"]) == 1
    assert out["shortfalls"][0]["target"] == 10


def test_a_leader_rule_applies_whatever_case_the_schedule_uses():
    rows = [_row("2026-09-12", "Saturday", "Pat", "BARTENDER")]
    rules = [{"role": "Bartender", "days": ["Saturday"], "daypart": "night",
              "min_score": 5}]
    out = labor.verify_shift_strength(rows, {"Pat": 3}, {}, leader_rules=rules)
    assert len(out["leader_misses"]) == 1


def test_an_unreachable_target_is_still_caught_on_a_case_mismatch():
    """Validation asked "could this team ever reach that number" with an
    exact role match, so a case difference compared the target against an
    empty team and said nothing at all.

    Asserted on the warning rather than its absence deliberately: an empty
    warning list is what the broken version returns too, so a test that
    only checks for silence passes either way.
    """
    problems = validate_strength_thresholds(
        {"Bartender": 20}, {"Pat": 5, "Sam": 4}, {"Pat": "bartender", "Sam": "bartender"})
    assert any("can never be met" in p for p in problems), problems


def test_a_reachable_target_is_not_warned_about_on_a_case_mismatch():
    problems = validate_strength_thresholds(
        {"Bartender": 8}, {"Pat": 5, "Sam": 4}, {"Pat": "bartender", "Sam": "bartender"})
    assert problems == []


# ── Both surfaces render it, not merely receive it ─────────────────────────
#
# The recurring failure across these audits is a figure computed carefully,
# returned in the payload, and decoded by nothing. Comments are stripped
# before asserting, because a name appearing only in a comment explaining
# the feature would otherwise satisfy the check.

def _source(*parts):
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent.joinpath(*parts)).read_text()


def _no_comments(text, markers=("//",)):
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if any(stripped.startswith(m) for m in markers):
            continue
        out.append(line)
    body = "\n".join(out)
    for opener, closer in (("<!--", "-->"),):
        while opener in body and closer in body[body.index(opener):]:
            start = body.index(opener)
            end = body.index(closer, start) + len(closer)
            body = body[:start] + body[end:]
    return body


def test_ios_decodes_the_strength_check():
    swift = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift"))
    for field in ("let strength: ScheduleStrength?", "leader_misses", "short_by",
                  "struct ScheduleStrength"):
        assert field in swift, f"{field} is returned by the backend and decoded by nothing"


def test_the_legacy_strength_banner_is_gone_from_both_surfaces():
    """It ran a second leadership check with different semantics from the
    quality engine — silently dropping any rule without a minimum score,
    which the engine enforces — and both rendered, so an owner read "every
    target met" a few centimetres above "needs 2 bartenders, found 1"."""
    swift = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift"))
    assert "strengthBanner" not in swift
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "renderScheduleStrength" not in html
    assert "sched-strength-banner" not in html


def test_ios_has_a_rating_control_that_can_clear_a_rating():
    swift = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/TeamStrengthSection.swift"))
    assert "labor/team/rating" in _no_comments(
        _source("ios/CavnarAI/CavnarAI/Features/Labor/LaborViewModel.swift"))
    # Tapping the current score clears it — not rated is a real state.
    assert "member.score == value ? nil : value" in swift


def test_ios_mounts_the_two_new_sections():
    swift = _no_comments(_source("ios/CavnarAI/CavnarAI/Features/Labor/LaborView.swift"))
    assert "TeamStrengthSection(viewModel: viewModel)" in swift
    assert "ShiftTargetsSection(viewModel: viewModel)" in swift
    assert "viewModel.loadTeam()" in swift


def test_leadership_is_reported_by_the_one_engine_that_owns_it():
    """Everything the retired banner said is covered by the quality panel's
    leadership dimension, which is what now renders it."""
    html = _no_comments(_source("templates", "dashboard.html"))
    assert 'id="sched-quality"' in html
    engine = _source("shift_quality.py")
    assert "def dim_leadership" in engine and '"leadership": dim_leadership' in engine


def test_the_web_dashboard_has_the_rating_panel():
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "toggleTeamPanel()" in html
    assert "'/api/labor/team/rating'" in html
    assert "'/api/labor/team/thresholds'" in html
    assert 'id="team-list"' in html and 'id="team-thresholds"' in html


def test_the_web_rating_control_clears_on_a_second_tap():
    html = _no_comments(_source("templates", "dashboard.html"))
    assert "(m.score === score) ? null : score" in html
