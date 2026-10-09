"""The memory round's UI wave, iOS part B (UI-IB, 9/29/26) — the iOS twin of
the web's Labor & Schedule and people, Reviews, Marketing, Food Cost, Intel,
What Connects and Events & reservations additions.

Two kinds of check, because the suite cannot run the app:
- source rules read from the Swift: each new element reads the field the
  server sends, by the server's own key, and the phone calls the route the
  server has (the decoders have XCTest cover too —
  CavnarAITests/MemoryRoundIBTests.swift, compiled by build-for-testing);
- behaviour on the routes the phone calls: the payload the decoders read
  carries those keys, with the types they expect. A renamed field fails
  here, not on Will's phone.
"""
import os
import re
import types

import pytest
from flask import Flask

import analyser
import auth
import mobile_api
import models
import staff_settings
import strategy_routes
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")
TESTS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAITests")


def _swift(rel):
    with open(os.path.join(APP, rel), encoding="utf-8") as fh:
        return fh.read()


# ── every new element reads the server's own key ────────────────────────────

# file → the server keys its decoder names (as Swift string literals).
DECODED_KEYS = {
    "Models/MemoryRecall.swift": [
        # scheduling notes
        "stale_after_days", "can_edit", "employee_name", "noted_on", "expires_on",
        # identity and mentions
        "can_answer", "person_id", "can_confirm", "date_iso", "review_id",
        # the person record
        "no_show_rate", "last_miss",
        # standing patterns and conflicts
        "first_learned", "last_confirmed", "times_applied", "times_overridden", "can_be_rule", "off_by", "on_by",
        # suppression, capability changes
        "review_on", "changed_by", "changed_at",
        # events
        "median_lift_pct",
        # reviews, marketing
        "window_days", "back_per_100", "came_back",
        # food cost
        "link_key", "ingredient_id", "suggested_par", "bias_pct", "prime_cost_pct", "projected_prime_cost",
        # intel
        "place_id", "from_rating", "to_rating", "observed_on", "review_count",
        # what connects
        "first_seen", "last_seen", "times_seen", "weeks_running",
    ],
    "Features/People/PersonSheet.swift": ["roles_held", "guest_mentions"],
    "Features/Labor/ScheduleSetupViewModel.swift": ["recommendation_suppression", "what_nights_teach"],
    "Features/Labor/LaborViewModel.swift": ["soft_requirements", "pattern_conflicts", "labor_target_label",
                                            "labor_target_source", "detail_thinned_at", "over_margin",
                                            "over_margin_basis"],
    "Features/Reviews/ReviewsAnalyticsViewModel.swift": ["controls_withheld"],
    "Features/Marketing/MarketingViewModel.swift": ["draft_ref", "content_log_id"],
    "Features/Marketing/MarketingComposeViewModel.swift": ["draft_ref", "content_log_id"],
    "Features/Marketing/GuestTextClubViewModel.swift": ["draft_ref", "returns_by_segment"],
    "Models/SupplierOrder.swift": ["owner_adjusted", "base_qty"],
    "Features/FoodCost/InvoiceScanSheet.swift": ["matched_by"],
    "Models/FoodCostAnalytics.swift": ["owner_ratio", "typical_price", "typical_monthly", "guard", "value_note",
                                       "projection_correction", "labor_basis_text", "controls_withheld"],
    "Features/Intel/IntelViewModel.swift": ["market_history", "own_rating_history"],
    "Features/Home/HomeFollowThrough.swift": ["link_memory"],
}


@pytest.mark.parametrize("rel", sorted(DECODED_KEYS))
def test_each_decoder_reads_the_servers_key(rel):
    src = _swift(rel)
    missing = [k for k in DECODED_KEYS[rel] if f'"{k}"' not in src]
    assert not missing, f"{rel} no longer decodes {missing}"


# Swift source → the mobile routes it calls.
ROUTES = {
    "Features/Labor/TeamMemorySection.swift": ["/mobile/api/labor/staff-notes", "/mobile/api/people/identity",
                                               "/mobile/api/people/mentions", "/mobile/api/labor/capability-changes",
                                               "/cover-answer"],
    "Features/People/PersonSheet.swift": ['"/roles"'],
    "Features/Labor/ScheduleSetupViewModel.swift": ["/mobile/api/labor/learned-patterns"],
    "Features/Reviews/ReviewRetagSheet.swift": ["/retag"],
    "Features/FoodCost/FoodCostAnalyticsViewModel.swift": ["/mobile/api/food-cost/par-suggestions", "/accept"],
}

# Server route → where it is declared (strategy_routes serves /api and
# /mobile/api from one table; capability-changes is mobile_api's own).
SERVER_ROUTES = [
    ("strategy_routes.py", '"/labor/staff-notes"'),
    ("strategy_routes.py", '"/labor/staff-notes/<int:note_id>"'),
    ("strategy_routes.py", '"/people/identity"'),
    ("strategy_routes.py", '"/people/identity/<int:question_id>"'),
    ("strategy_routes.py", '"/people/mentions"'),
    ("strategy_routes.py", '"/people/mentions/<int:signal_id>"'),
    ("strategy_routes.py", '"/people/<key>/roles"'),
    ("strategy_routes.py", '"/issues/<int:issue_id>/cover-answer"'),
    ("strategy_routes.py", '"/labor/learned-patterns"'),
    ("strategy_routes.py", '"/reviews/<int:review_id>/retag"'),
    ("strategy_routes.py", '"/food-cost/par-suggestions"'),
    ("strategy_routes.py", '"/food-cost/par-suggestions/<int:ingredient_id>/accept"'),
    ("mobile_api.py", '@mobile_bp.route("/labor/capability-changes")'),
]


@pytest.mark.parametrize("rel", sorted(ROUTES))
def test_the_phone_calls_each_new_route(rel):
    src = _swift(rel)
    missing = [p for p in ROUTES[rel] if p not in src]
    assert not missing, f"{rel} no longer calls {missing}"


@pytest.mark.parametrize("module,decl", SERVER_ROUTES)
def test_each_route_the_phone_calls_is_served(module, decl):
    with open(os.path.join(ROOT, module), encoding="utf-8") as fh:
        assert decl in fh.read(), f"{module} no longer declares {decl}"


def test_the_strategy_table_serves_both_surfaces():
    """One table, both prefixes: the phone's /mobile/api twin is registered
    for every path above."""
    paths = {p for p, _m, _fn, _e in strategy_routes._ROUTES}
    for p in ("/labor/staff-notes", "/people/identity", "/people/mentions", "/people/<key>/roles",
              "/issues/<int:issue_id>/cover-answer", "/reviews/<int:review_id>/retag",
              "/food-cost/par-suggestions", "/food-cost/par-suggestions/<int:ingredient_id>/accept"):
        assert p in paths


# ── what each screen renders from it ────────────────────────────────────────

def test_labor_setup_holds_the_team_memory_and_needs_you_nudges_it():
    view = _swift("Features/Labor/LaborView.swift")
    assert "TeamMemorySection(viewModel: teamMemory" in view
    assert "TeamMemoryNudge(viewModel: teamMemory)" in view
    assert "await teamMemory.load()" in view
    sec = _swift("Features/Labor/TeamMemorySection.swift")
    # Still true? on a stale part; one constraint per answer, never a whole note.
    assert '"Still true?"' in sec and 'action: "confirm"' in sec and 'action: "expire"' in sec
    assert "part: part.index" in sec
    assert '"Same person"' in sec and '"Different people"' in sec
    assert "part.dateLine" in sec and ".strikethrough(part.ended" in sec


def test_the_person_sheet_says_unwatched_attendance_is_unknown():
    rec = _swift("Models/MemoryRecall.swift")
    assert '"Not watched yet"' in rec
    sheet = _swift("Features/People/PersonSheet.swift")
    assert 'AccountSection(kicker: "What Cavnar AI knows")' in sheet
    assert "person.attendance ?? PersonAttendance(known: false)" in sheet


def test_standing_patterns_offer_make_it_a_rule():
    roster = _swift("Features/Labor/RosterSection.swift")
    assert "standingPatternsBlock" in roster and '"Make it a rule"' in roster
    assert "pattern.canBeRule" in roster
    vm = _swift("Features/Labor/ScheduleSetupViewModel.swift")
    assert "RuleBody(key: pattern.key, rule: true)" in vm


def test_the_draft_shows_soft_requirements_applied_or_not():
    notes = _swift("Features/Labor/ScheduleWeekNotes.swift")
    assert "softRequirementsBlock" in notes and "req.appliedLabel" in notes
    assert "conflictsBlock" in notes and "Self.targetLine" in notes


def test_the_schedule_panels_say_pass():
    # Owner, 10/1/26: "Pass" everywhere.
    for rel in ("Features/Labor/ScheduleReviewPanel.swift", "Features/Labor/ShiftQualityPanel.swift"):
        src = _swift(rel)
        assert 'Text("Pass")' in src or 'label: "Pass"' in src
        assert 'Text("Not for us")' not in src and 'label: "Not for us"' not in src


def test_older_diagnoses_lose_their_controls():
    rv = _swift("Features/Reviews/ReviewsAnalyticsSection.swift")
    assert "d.showsControls" in rv and "d.isOlderRead" in rv
    fc = _swift("Features/FoodCost/FoodCostAnalyticsSection.swift")
    assert "dg?.showsControls == true" in fc


def test_a_guarded_reprice_steps_down_and_says_why():
    fc = _swift("Features/FoodCost/FoodCostAnalyticsSection.swift")
    assert 'CavnarCaveat(title: "Fix the plate before the price", detail: g.text)' in fc
    assert "RepriceButtonStyle(guarded: s.repriceGuard != nil" in fc
    assert "s.typicalLine" in fc and "s.valueNote" in fc
    assert "parSection(viewModel.parSuggestions)" in fc and 'answers: [.notForUs]' in fc


def test_marketing_sends_the_draft_back():
    mk = _swift("Features/Marketing/MarketingViewModel.swift")
    assert mk.count("contentLogId: contentLogId, draftRef: draftRef") >= 3     # IG, FB, Google
    assert "draftRef: viewModel.draftRef" in _swift("Features/Marketing/MarketingView.swift")
    gt = _swift("Features/Marketing/GuestTextClubViewModel.swift")
    assert "draftRef: draftRef)" in gt


def test_intel_and_what_connects_show_their_history():
    # iOS readability round (10/8/26): the phone keeps "What changed" and
    # links to the history chart on the web ("Web explains. iPhone decides.").
    intel = _swift("Features/Intel/IntelView.swift")
    assert "movementSection(movement)" in intel
    assert 'CavnarWebLinkRow(title: "Ratings over time"' in intel
    sheet = _swift("Features/Home/HomeOneThingCard.swift")
    assert "link.memory" in sheet and "memory.badge" in sheet


# ── the review re-tag vocabulary is the analyser's ─────────────────────────

def _swift_pairs(src, name):
    m = re.search(r"static let " + name + r": \[\(key: String, label: String\)\] = \[(.*?)\n    \]", src, re.S)
    assert m, f"ReviewTagVocabulary.{name} is gone"
    return re.findall(r'\("([^"]+)", "([^"]+)"\)', m.group(1))


def test_the_retag_vocabulary_matches_the_analyser():
    src = _swift("Models/MemoryRecall.swift")
    assert _swift_pairs(src, "categories") == [(k, analyser.CATEGORY_LABELS[k]) for k in analyser.CATEGORIES]
    assert _swift_pairs(src, "severities") == [(k, analyser.SEVERITY_LABELS[k]) for k in analyser.SEVERITIES]
    sents = re.search(r"static let sentiments: \[String\] = \[(.*?)\]", src).group(1)
    assert re.findall(r'"([^"]+)"', sents) == list(analyser.SENTIMENTS)


# ── house rules on the new Swift ────────────────────────────────────────────

NEW_SWIFT = ["Models/MemoryRecall.swift", "Features/Labor/TeamMemorySection.swift",
             "Features/Reviews/ReviewRetagSheet.swift", "Features/Intel/IntelHistorySection.swift"]


@pytest.mark.parametrize("rel", NEW_SWIFT)
def test_new_swift_keeps_the_house_rules(rel):
    src = _swift(rel)
    assert "ProgressView(" not in src, "loading is the pulse or the shimmer, never a spinner"
    assert "DateFormatter" not in src and '"MMM' not in src, "dates an owner reads are CavnarDate M/D/YY"
    # The product is "Cavnar AI" in anything an owner reads.
    for lit in re.findall(r'"((?:[^"\\]|\\.)*)"', src):
        assert not re.search(r"\bCavnar\b(?! AI)", lit), f"bare 'Cavnar' in {lit!r}"


def test_the_xctest_cover_is_in_the_test_target():
    with open(os.path.join(TESTS, "MemoryRoundIBTests.swift"), encoding="utf-8") as fh:
        src = fh.read()
    for name in ("StaffNotesPayload", "IdentityPayload", "MentionsPayload", "PersonRecord", "StandingPattern",
                 "GeneratedSchedule", "SuppressionMap", "RequestConversion", "SegmentReturn", "ParSuggestion",
                 "RepriceSuggestions", "InvoiceLine", "IntelMovement", "CrossModule"):
        assert name in src


# ── the payloads the phone reads carry those keys ───────────────────────────

@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    import action_queue
    monkeypatch.setattr(action_queue, "get_conn", fake)
    return db_path


@pytest.fixture
def phone(db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Phone Co", owner_email="p@x.test", module_labor=1,
                                       module_inventory=1, module_reviews=1))
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "owner",
            "is_admin": 0, "email": "p@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    c = app.test_client()
    c.rid = rid
    return c


def _get(phone, path):
    r = phone.get("/mobile/api" + path, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:300])
    return r.get_json()


def test_the_staff_notes_payload_is_what_the_phone_decodes(phone):
    models.save_staff_note(phone.rid, "Luis R.", "mornings only")
    body = _get(phone, "/labor/staff-notes")
    assert {"notes", "stale_after_days", "stale", "can_edit"} <= set(body)
    note = body["notes"][0]
    assert isinstance(note["id"], int) and note["employee_name"] == "Luis R."
    part = note["parts"][0]
    assert {"index", "text", "noted", "noted_on", "expires", "expires_on", "ended", "stale"} <= set(part)
    assert "/" in part["noted"] and "-" not in part["noted"]         # M/D/YY for the eye


def test_the_identity_mentions_and_patterns_payloads_carry_their_lists(phone):
    ident = _get(phone, "/people/identity")
    assert isinstance(ident["questions"], list) and isinstance(ident["can_answer"], bool)
    ment = _get(phone, "/people/mentions")
    assert isinstance(ment["mentions"], list) and isinstance(ment["can_confirm"], bool)
    pats = _get(phone, "/labor/learned-patterns")
    assert isinstance(pats["standing"], list) and isinstance(pats["conflicts"], list)


def test_the_person_record_carries_its_memory(phone):
    import shift_facts
    shift_facts.ingest(phone.rid, [{"date": "2026-09-01", "day": "Tuesday", "employee": "Ana B.", "role": "Server",
                                    "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6",
                                    "actual_hours": "6", "sales": "1000"}], "upload")
    person = _get(phone, "/people/ana-b")["person"]
    assert person["attendance"] == {"known": False}                  # "Not watched yet"
    assert person["covers"] == {"taken": 0, "declined": 0, "days": 180}
    assert person["roles_held"] == [] and person["guest_mentions"] == []
    added = phone.post("/mobile/api/people/ana-b/roles", json={"role": "Bartender", "since": "2026-09-01"},
                       headers={"Authorization": "Bearer t"})
    assert added.status_code == 200, added.get_data(as_text=True)
    held = _get(phone, "/people/ana-b")["person"]["roles_held"]
    assert held and {"role", "since", "primary"} <= set(held[0])


def test_the_par_suggestions_payload_is_a_list(phone):
    body = _get(phone, "/food-cost/par-suggestions")
    assert body["ok"] is True and isinstance(body["suggestions"], list)


def test_the_retag_route_takes_what_the_sheet_sends(phone, db):
    conn = models.get_conn()
    cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                       "sentiment, categories, severity, entities, review_date, fetched_at) VALUES "
                       "(?,?,?,?,?,?,1,'negative','[\"wait_time\"]','minor','{}',datetime('now'),datetime('now'))",
                       (phone.rid, "google", "g-1", "Maria G.", 2, "The pasta was cold"))
    conn.commit()
    rv = cur.lastrowid
    conn.close()
    # The sheet's body: categories as keys, sentiment and severity as keys,
    # dishes lowercased — only what changed.
    r = phone.post(f"/mobile/api/reviews/{rv}/retag",
                   json={"categories": ["food_quality", "value"], "severity": "service", "dishes": ["carbonara"]},
                   headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, r.get_data(as_text=True)
    out = r.get_json()
    assert set(out["changed"]) == {"categories", "severity", "dishes"}
    assert {"categories", "sentiment", "severity", "dishes"} <= set(out["review"])   # ReviewTags


def _labor_payload(monkeypatch, analysis):
    monkeypatch.setattr(mobile_api, "analyse_shifts_for_restaurant", lambda rid: analysis, raising=False)
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda rid: analysis)
    monkeypatch.setattr(mobile_api, "get_restaurant",
                        lambda rid: types.SimpleNamespace(labor_target_pct=30.0, hourly_rate=18.5,
                                                          timezone="America/Chicago", category="italian"))
    monkeypatch.setattr(mobile_api, "_staff_constraints_index", lambda rid: {})
    payload, _ = mobile_api._do_mobile_labor(1)
    return payload


def _analysis(**over):
    a = {"is_live": True, "overall_labor_pct": 28.0, "total_sales": 60000.0, "period_days": 14,
         "employee_hours": {}, "overtime_risk": [], "role_summary": {},
         "date_range": {"start": "2026-09-01", "end": "2026-09-14", "days": 14},
         "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}, "potential_savings": 0,
         "potential_savings_monthly": 0, "sales_data_missing": False, "days_missing_sales": [],
         "hours_are_estimated": False, "days_with_conflicting_sales": [], "duplicate_rows_ignored": 0,
         "period_too_short_to_project": False, "overtime_hours": 0, "overtime_premium": 0.0}
    a.update(over)
    return a


def test_mobile_labor_carries_the_fitted_margin_on_live_shifts_only(monkeypatch):
    basis = "1.645× this restaurant's own spread of daily labor % (σ 2.1 points over 60 days)"
    live = _labor_payload(monkeypatch, _analysis(over_margin=3.45, over_margin_basis=basis))
    assert live["over_margin"] == 3.45 and live["over_margin_basis"] == basis
    sample = _labor_payload(monkeypatch, _analysis(is_live=False, over_margin=3.0, over_margin_basis="x"))
    assert sample["over_margin"] is None and sample["over_margin_basis"] is None


def test_the_labor_trend_rows_say_whether_the_week_is_complete():
    with open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("def mobile_labor_trend"):src.index("def mobile_labor_gap")]
    assert '"complete": bool(h.get("complete"))' in body
    assert "var complete: Bool?" in _swift("Features/Labor/LaborAnalyticsViewModel.swift")
