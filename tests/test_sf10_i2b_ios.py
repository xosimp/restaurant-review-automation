"""The iPhone catches up with the scheduler fix round (10/10/26, workstream
I2b): every payload the phone now reads is read by the keys the server
writes, and every write it now makes goes to a route that exists with the
keys that route reads.

Source-level on purpose (the Swift is compiled by xcodebuild; the decode
logic is unit-tested in ios/CavnarAI/CavnarAITests/SchedFixI2bTests.swift):
these hold the two sides of each wire together, so a renamed server key or
a dropped Swift CodingKey fails here, in the Python suite, before a phone
shows an empty card.

* TRUST-27 / RM-6 — the Send sheet reads the ONE pre-publish payload
  (`labor/publish-review`, contract #6 as I1 published it) and sends each
  must-fix's override by key with its reason (`overrides: [{key, reason,
  covered_by?}]`); hard items are red, never amber for everything.
* TRUST-12 — a Crew screen and the Send sheet's leaner-than-usual cells from
  `/mobile/api/labor/crew-matrix`, in the restaurant's own week order.
* Contract #4 — a shift's lock is the row's `lock`, sent only by the owner.
* ARCH-2 — the phone's Stop, and the restored draft read again.
* SCALE-29 — the re-score asks for and merges the quality delta.
* LOGIC-29 / TRUST-21 / ARCH-12 — the full-time line, the closing-stay one
  tap and the lunch/dinner boundary on the phone.
* MARKET-2/3/4 — the staff app's consent tap after a 409 needs_consent, its
  Agreements (consents, estimate signing) and the owner's labor-laws screen.
* J2 — On the floor, the floor switches and the POS push preview, the staff
  app's other-locations opt-in.
* BUGSLIFE-5 — approved time off over a published shift, with Post it open.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")
LABOR = os.path.join(APP, "Features", "Labor")
STAFF = os.path.join(APP, "Features", "Staff")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _swift(name, folder=LABOR):
    return _read(folder, name)


def _py(name):
    return _read(ROOT, name)


def _raw_keys(src, type_name):
    """The JSON keys a Swift type's CodingKeys spell (raw values, or the case
    names where no raw value is given), for every CodingKey enum inside the
    type's own body."""
    start = src.index(f"struct {type_name}")
    depth, i, body_start = 0, src.index("{", start), None
    body_start = i
    while True:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = src[body_start:i]
    keys = set()
    for enum in re.finditer(r"enum \w+: String, CodingKey \{(.*?)\n\s*\}", body, re.S):
        for line in enum.group(1).split("\n"):
            line = line.strip()
            if not line.startswith("case "):
                continue
            for part in line[5:].split(","):
                part = part.strip()
                if not part:
                    continue
                m = re.match(r'(\w+)\s*=\s*"([^"]+)"', part)
                keys.add(m.group(2) if m else part.split()[0])
    return keys


# ── the pre-publish review ──────────────────────────────────────────────────

def test_the_send_sheet_reads_the_one_review_and_sends_each_override_by_key():
    sheet = _swift("PublishScheduleSheet.swift")
    review = _swift("PrePublishReview.swift")
    assert '"/mobile/api/labor/publish-review"' in sheet
    # Every top-level part of contract #6 is read.
    keys = _raw_keys(review, "PublishReview: Decodable")
    for k in ("ok", "schedule_id", "week_start", "week_end", "verdict", "must_fix", "decide", "numbers", "changed",
              "fragile", "why", "sure", "send", "version", "quality", "fyi"):
        assert k in keys, k
    # A must-fix's override names what it needs; a manager gap is sent past
    # only by naming who runs the floor (ARCH-30).
    for k in ("override_allowed", "override", "overridden_before", "tier"):
        assert k in review, k
    assert '"covered_by"' in review and "case coveredBy = \"covered_by\"" in review
    # The publish body carries them, and the route reads that key.
    assert "case acknowledge, version, resend, overrides" in sheet
    assert 'data.get("overrides")' in _py("client_api.py")
    # One switch for the whole list is gone: each item is fixed or
    # overridden on its own (TRUST-2/3).
    assert "I've read these — send anyway" not in sheet
    assert "acknowledgeBlockers" not in sheet
    # Hard items are red on the phone too (TRUST-27).
    views = _swift("PrePublishReviewViews.swift")
    assert "item.hard ? .cavnarRed : .cavnarAmber" in views
    # The review's actions as I1 published them.
    for action in ("edit_rows", "acknowledge", "override", "optimize", "apply_fixes"):
        assert f'"{action}"' in review + sheet + views, action
    assert '"/mobile/api/labor/standby/ask"' in sheet and '"/mobile/api/labor/crew-matrix"' in sheet


def test_the_summary_leads_with_the_verdict_never_a_celebration():
    week = _swift("ScheduleWeekViews.swift")
    assert 'Text("The week is built")' not in week
    assert "PublishVerdictCard(verdict: verdict)" in week
    assert '"/mobile/api/labor/publish-review"' in week


def test_the_publish_route_reads_overrides_and_the_phone_sends_them_in_every_publish_body():
    # The parity test holds the union; here the sheet's own body carries it.
    sheet = _swift("PublishScheduleSheet.swift")
    assert re.search(r"overrides: chosen\)\)", sheet)


# ── Crew ───────────────────────────────────────────────────────────────────

def test_crew_reads_the_crew_matrix_by_the_keys_it_writes():
    crew = _swift("CrewMatrixScreen.swift")
    src = _py("crew_matrix.py")
    assert '"/mobile/api/labor/crew-matrix"' in crew
    for k in _raw_keys(crew, "Cell: Decodable"):
        assert f'"{k}"' in src, f"crew cell key {k!r} is not in crew_matrix.build"
    for k in _raw_keys(crew, "CrewMatrix: Decodable") - {"error", "message"}:
        assert f'"{k}"' in src, f"crew key {k!r} is not in crew_matrix.build"
    # The restaurant's own week order, never a Monday-first list when sent.
    assert "days.isEmpty ?" in crew and '"days": week_day_order(' in src
    # Every answer the route takes.
    for choice in ('"hold"', '"lean"', '"clear"', '"add"'):
        assert choice in crew, choice
    assert 'CHOICES = ANSWERS + ("clear", "add")' in src


def test_the_crew_line_is_on_labor_and_on_the_send_sheet():
    assert "CrewMatrixScreen(historyId:" in _swift("LaborView.swift")
    assert "PublishCrewCard(store: viewModel.crew" in _swift("PublishScheduleSheet.swift")


# ── locks, stop, delta ─────────────────────────────────────────────────────

def test_a_lock_is_the_rows_lock_and_the_save_reads_it():
    vm = _swift("LaborViewModel.swift")
    assert "case locked, lock" in vm
    assert "func setShiftLocked(rowId: String, locked: Bool)" in vm
    assert 'Lock this shift' in _swift("ShiftEditSheet.swift")
    assert '_raw.get("lock")' in _py("mobile_api.py")
    # A move keeps the shift's id, person and lock (contract #3).
    assert "shiftId: was.shiftId, personId: was.personId" in vm
    # A stored week's "1" reads as locked.
    assert '["1", "true", "yes", "y", "on"]' in vm


def test_stop_is_on_the_phone_and_the_restored_draft_is_read_again():
    vm = _swift("LaborViewModel.swift")
    assert '"/mobile/api/labor/generate-schedule/stop"' in vm
    assert 'case restoredHistoryId = "restored_history_id"' in vm
    assert "await reloadAfterStop(back)" in vm
    assert '"restored_history_id": int(prior)' in _py("schedule_engine.py")


def test_the_rescore_asks_for_and_merges_the_quality_delta():
    vm = _swift("LaborViewModel.swift")
    assert 'case qualityEtag = "quality_etag"' in vm and "case rows, save, version, delta" in vm
    assert 'case qualityDelta = "quality_delta"' in vm
    assert "static func mergeQuality(held: ScheduleQuality, delta: QualityDelta)" in vm
    # The key order is the server's own.
    assert "f\"{(s or {}).get('date') or ''}|{(s or {}).get('daypart') or ''}\"" in _py("schedule_engine.py")
    assert 'let k = "\\(s.date)|\\(s.daypart)"' in vm
    # The save's idempotency key is made before the delta is asked for.
    i_key = vm.index("body.idempotencyKey = Self.saveKey(body)")
    i_delta = vm.index("body = askingForDelta(body)")
    assert i_key < i_delta


# ── rules, stays, the full-time line ───────────────────────────────────────

def test_the_rules_send_the_boundary_and_the_full_time_switch():
    setup = _swift("ScheduleSetupViewModel.swift")
    sheet = _swift("ScheduleRulesSheet.swift")
    assert 'case daypartBoundary = "daypart_boundary"' in setup
    assert '"Lunch ends at"' in sheet
    assert 'rules["full_time_min_confirmed"] = .bool(fullTimeMinConfirmed)' in sheet
    assert '"full_time_min_confirmed"' in _py("schedule_rules.py")


def test_the_closing_stay_and_the_full_time_line_are_one_tap_on_the_phone():
    vm = _swift("LaborViewModel.swift")
    assert 'case assumedStays = "assumed_stays"' in vm
    assert '"extend_shift"' in vm
    assert 'result["review"]["assumed_stays"] = _stays' in _py("schedule_engine.py")
    assert 'item.kind == "full_time_unconfirmed"' in _swift("ScheduleFixViews.swift")
    assert '"kind": "full_time_unconfirmed"' in _py("schedule_setup.py")


# ── the staff app and the labor laws ───────────────────────────────────────

def test_the_staff_app_asks_for_consent_in_the_same_flow():
    req = _swift("StaffRequestsViews.swift", STAFF)
    agr = _swift("StaffAgreementsSection.swift", STAFF)
    assert "StaffNeedsConsent.from(error)" in req
    assert 'case consentRest = "consent_rest"' in agr
    assert 'body.get("consent_rest") is True' in _py("staff_routes.py")
    for path in ('"/staff/api/compliance"', '"/staff/api/consents/rest-standing"', '"/staff/api/travel"'):
        assert path in agr, path
    assert "/staff/api/estimate/\\(est.id)/acknowledge" in agr
    assert "StaffAgreementsSection()" in _swift("StaffMeView.swift", STAFF)


def test_the_owner_has_the_labor_laws_on_the_phone():
    screen = _swift("ComplianceScreen.swift")
    for path in ('"/mobile/api/labor/compliance"', '"/mobile/api/labor/compliance/profile"',
                 '"/mobile/api/labor/compliance/estimates/draft"', '"/mobile/api/labor/compliance/estimates"'):
        assert path in screen, path
    routes = _py("strategy_routes.py")
    for r in ('"/labor/compliance"', '"/labor/compliance/profile"', '"/labor/compliance/estimates/draft"',
              '"/labor/compliance/requests/<int:request_id>/answer"'):
        assert r in routes, r
    assert "ComplianceScreen()" in _swift("ScheduleRulesSheet.swift")


def test_on_the_floor_reads_the_live_labor_by_its_keys():
    card = _swift("OnTheFloorCard.swift")
    live = _py("labor_live.py")
    for k in ("so_far", "projected", "to_date", "hourly_so_far", "all_in_projected", "target_label", "over_by",
              "moves_today", "can_act"):
        # Read by the phone (a raw value, or a case named as the key)...
        assert f'"{k}"' in card or re.search(r"case [^\n]*\b%s\b" % k, card), k
        # ...and written by the server.
        assert f'"{k}"' in live or f'"{k}"' in _py("floor_api.py"), k
    for path in ('"/mobile/api/labor/live"', '"/mobile/api/labor/floor-settings"',
                 '"/mobile/api/labor/pos-schedule-push"'):
        assert path in card, path
    # The POS push is switched on only from its preview.
    assert "posPush: true, previewed: true" in card
    assert 'if not b.get("previewed")' in _py("floor_api.py")
    assert "OnTheFloorCard(store: floorStore)" in _swift("LaborView.swift")


def test_approved_time_off_over_a_published_shift_waits_with_post_it_open():
    t = _swift("TimeOffSection.swift")
    for k in ("request_id", "employee_name", "shift_start", "shift_end"):
        assert f'"{k}"' in t, k
    assert 'case followUps = "follow_ups"' in _swift("LaborViewModel.swift")
    assert 'Text("Post it open")' in _swift("LaborWaitingOnYou.swift")
    assert '"follow_ups"' in _py("strategy_routes.py")
