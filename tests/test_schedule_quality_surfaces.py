"""The owner-facing surfaces for the Shift Quality optimizer and what the
draft learns, on web and iOS.

Read at the source, as tests/test_edge_sched_frontend.py does: each feature
must be mounted and must call the endpoint the backend exposes for it. The
backend contract itself is pinned in tests/test_schedule_optimizer.py and
tests/test_schedule_learning.py.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABOR = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Labor")


def _html():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def _swift(name):
    with open(os.path.join(LABOR, name), encoding="utf-8") as f:
        return f.read()


def _function(src, name):
    start = src.index(f"function {name}(")
    nxt = re.compile(r"\nfunction \w+\(").search(src, start + 10)
    return src[start:nxt.start() if nxt else len(src)]


# ── 1. What the optimizer changed ─────────────────────────────────────────

def test_web_shows_what_cavnar_changed_and_what_still_needs_the_owner():
    html = _html()
    assert 'id="sq-optimizer"' in html
    body = _function(html, "renderQualityOptimizer")
    assert "Cavnar improved this draft from" in body
    assert "o.changes" in body and ".reason" in body
    assert "Still needs you" in body and "o.unresolved" in body
    # From generation, or from the stored quality when a week is reloaded.
    handle = _function(html, "_schedHandleResult")
    assert "data.optimizer || (data.quality && data.quality.optimizer)" in handle
    assert "_sqGate = data.gate" in handle


def test_ios_shows_what_cavnar_changed_and_what_still_needs_the_owner():
    panel = _swift("ShiftQualityPanel.swift")
    assert "optimizerBlock(" in panel and "STILL NEEDS YOU" in panel
    assert "optimizerSummary" in panel and "gate?.reason" in panel
    model = _swift("LaborViewModel.swift")
    assert "struct ScheduleOptimizer" in model and 'case beforeScore = "before_score"' in model
    assert "Cavnar improved this draft from" in model


# ── 2. Improve with Cavnar ─────────────────────────────────────────────────

def test_web_improve_with_cavnar_calls_optimize_and_saves_nothing():
    html = _html()
    assert "improveScheduleWithCavnar(this)" in html
    i = html.index("window.improveScheduleWithCavnar=function")
    body = html[i:html.index("\n  };\n", i)]
    assert "/api/labor/schedule/optimize" in body
    assert "save:true" not in body.replace(" ", "")
    assert "_schedSetDirty(true)" in body            # staged; Save or Discard is the owner's
    assert "'Cavnar:'" in body                        # the rows it touched are marked


def test_ios_improve_with_cavnar_calls_optimize_and_saves_nothing():
    model = _swift("LaborViewModel.swift")
    i = model.index("func optimize() async")
    body = model[i:model.index("\n    }\n", i)]
    assert "/mobile/api/labor/schedule/optimize" in body
    assert "hasUnsavedFixes = true" in body
    assert "Improve with Cavnar" in _swift("ScheduleReviewPanel.swift")


# ── 3. The score's movement ────────────────────────────────────────────────

def test_web_shows_the_points_an_edit_moved_the_score():
    html = _html()
    assert 'id="sq-delta"' in html
    assert "function _renderQualityDelta(prevScore, score)" in html
    rescore = _function(html, "rescoreSchedule")
    assert "var prevScore = _quality ? _quality.score : null;" in rescore
    assert "renderShiftQuality(d.quality, d.what_if, prevScore)" in rescore


def test_ios_shows_the_points_an_edit_moved_the_score():
    model = _swift("LaborViewModel.swift")
    i = model.index("private func performRescore(save: Bool) async")
    assert "scoreDelta = Self.delta(" in model[i:i + 3000]
    assert "ScoreDeltaChip(delta:" in _swift("ShiftQualityPanel.swift")


# ── 4. Confidence ─────────────────────────────────────────────────────────

def test_low_confidence_reads_provisional_with_its_top_reason():
    html = _html()
    body = _function(html, "renderQualityConfidence")
    assert "confidence.level === 'low'" in body and "provisional" in body
    assert "(confidence.reasons || [])[0]" in body
    # The history list carries it beside the score too.
    assert "r.confidence==='low'?' · provisional'" in html
    panel = _swift("ShiftQualityPanel.swift")
    assert "PROVISIONAL" in panel and "confidence.reasons.first" in panel


# ── 5. The capping dimension first ─────────────────────────────────────────

def test_the_capping_dimension_leads_the_shift_explanation():
    html = _html()
    body = _function(html, "_qualityShiftDetail")
    assert "shift.capped_by" in body and "holding the shift at" in body
    assert body.index("holding the shift at") < body.index("_qualityGroup('Working well'")
    panel = _swift("ShiftQualityPanel.swift")
    assert "cappingLines(shift)" in panel and "holding the shift at" in panel


# ── 6. Ratings that match nobody on the roster ─────────────────────────────

def test_unmatched_ratings_can_be_matched_on_both_platforms():
    html = _html()
    assert 'id="team-unmatched"' in html
    assert "/api/labor/ratings/unmatched" in html and "/api/labor/ratings/match" in html
    assert "roster_name: target" in html
    assert "match anyone on your roster" in html
    model = _swift("LaborViewModel.swift")
    assert "/mobile/api/labor/ratings/unmatched" in model and "/mobile/api/labor/ratings/match" in model
    assert 'case rosterName = "roster_name"' in model
    team = _swift("TeamStrengthSection.swift")
    assert "unmatchedBlock" in team and "loadUnmatchedRatings()" in team


# ── 7. Rating onboarding ──────────────────────────────────────────────────

def test_a_mostly_unrated_week_offers_to_rate_the_people_carrying_it():
    html = _html()
    assert 'id="sq-rate"' in html
    body = _function(html, "_sqUnrated")
    assert "have no Operational Score" in body and "slice(0, 10)" in body
    assert "scheduled_hours" in body
    assert "/api/labor/team/rating" in html[html.index("data-sq-rate"):]
    model = _swift("LaborViewModel.swift")
    assert "var unratedByHours" in model and "prefix(10)" in model
    assert "func rateFromPrompt" in model
    assert "ratePrompt(" in _swift("ShiftQualityPanel.swift")


# ── 8. Experienced ────────────────────────────────────────────────────────

def test_experienced_is_a_per_person_toggle_on_both_platforms():
    html = _html()
    assert "_lbSw('experienced'" in html and "Experienced — knows the job" in html
    assert "f==='experienced'" in html
    roster = _swift("RosterSection.swift")
    assert 'AccountSwitchRow(label: "Experienced — knows the job"' in roster
    assert "experienced: newValue" in roster
    setup = _swift("ScheduleSetupViewModel.swift")
    assert "try c.encodeIfPresent(experienced, forKey: .experienced)" in setup


# ── 9. What the draft learns ─────────────────────────────────────────────

def test_intel_shows_kept_share_calibration_and_the_auto_publish_offer():
    html = _html()
    i = html.index("function _intLearnHtml(d)")
    body = html[i:html.index("window.renderIntel=function", i)]
    for piece in ("draft_acceptance", "unchanged_share", "weight_calibration", "cal.reason",
                  "auto_publish_offer", "off.eligible", "acceptAutoPublishOffer(this)"):
        assert piece in body, piece
    assert "/api/labor/auto-publish" in body and "enabled:true" in body.replace(" ", "")
    intel = _swift("ScheduleIntelSection.swift")
    for piece in ("autoPublishOfferCard(", "acceptanceBlock(", "calibrationBlock("):
        assert piece in intel, piece
    setup = _swift("ScheduleSetupViewModel.swift")
    assert '"/mobile/api/labor/auto-publish"' in setup and "func acceptAutoPublishOffer" in setup


def test_calibrated_weights_are_shown_never_applied():
    """A suggested weight is the owner's to adopt. Nothing on either
    surface writes it back."""
    html = _html()
    i = html.index("function _intLearnHtml(d)")
    body = html[i:html.index("window.renderIntel=function", i)]
    assert "quality-weights" not in body
    assert "quality-weights" not in _swift("ScheduleIntelSection.swift")


# ── 10. What if ───────────────────────────────────────────────────────────

def test_what_if_scores_a_change_without_saving_it():
    html = _html()
    live = _function(html, "_sqLiveScore")
    assert "/api/labor/schedule/score" in live and "save: false" in live
    assert "_sqWhatIfHtml(shift)" in _function(html, "_qualityShiftDetail")
    assert "What if" in _function(html, "_sqWhatIfHtml")
    model = _swift("LaborViewModel.swift")
    i = model.index("func liveScore(rows: [ScheduleRow])")
    assert "save: false" in model[i:i + 800]
    assert "whatIfRow(shift" in _swift("ShiftQualityPanel.swift")


def test_every_new_web_button_carries_the_button_system():
    html = _html()
    for marker in ("data-sq-rate=", "data-sq-wi-try=", "data-rt-match=", "improveScheduleWithCavnar(this)",
                   "acceptAutoPublishOffer(this)", "rescoreQualityLive(this)"):
        i = html.index(marker)
        tag = html[html.rfind("<button", 0, i):i]
        assert "cbtn" in tag, marker
