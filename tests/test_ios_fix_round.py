"""The iOS fix round after the 9/25/26 blind re-audit (F3-1 … F3-18, D3-8,
D3-9, D3-13) — the server halves it needed and the rules that must hold in
the Swift source whatever a screen is rendered with.

The Swift logic has XCTest cover too (CavnarAITests/IOSFixRoundTests.swift);
these read the sources because the suite cannot run the app.
"""
import os
import re

import subprocess
import sys
import tempfile

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
APP = os.path.join(IOS, "CavnarAI")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _swift_files():
    for base in (APP, os.path.join(IOS, "Shared"), os.path.join(IOS, "CavnarWidgets")):
        for dirpath, _, names in os.walk(base):
            for n in names:
                if n.endswith(".swift"):
                    yield os.path.join(dirpath, n)


# ── F3-6 / D3-8: a background read is not the owner seeing anything ─────────

def test_the_queue_read_with_peek_presents_nothing(db_path, monkeypatch):
    import action_queue
    import rec_delivery
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="Peek Co", owner_email="p@x.test"), db_path=db_path)
    viewer = {"id": 7, "restaurant_id": rid, "role": "client", "is_admin": 0}
    calls = []
    monkeypatch.setattr(rec_delivery, "present_now", lambda *a, **k: calls.append(a))
    out = action_queue.items(rid, viewer=viewer, db_path=db_path, present=False)
    assert "items" in out and calls == []
    action_queue.items(rid, viewer=viewer, db_path=db_path)
    assert len(calls) == 1, "a real read still presents"


def test_the_actions_route_passes_peek_through(monkeypatch):
    import action_queue
    import strategy_routes
    seen = []
    monkeypatch.setattr(action_queue, "items", lambda *a, **k: seen.append(k.get("present")) or {"items": []})
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: None)
    u = {"id": 7, "restaurant_id": 1, "role": "client", "is_admin": 0}
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/actions?peek=1"):
        strategy_routes._do_actions(u)
    with app.test_request_context("/mobile/api/actions"):
        strategy_routes._do_actions(u)
    assert seen == [False, True]


def test_the_report_read_with_peek_presents_nothing(monkeypatch):
    import models
    import strategy_routes
    from dsr import access, store
    presented = []
    monkeypatch.setattr(strategy_routes, "_dsr_view", lambda u: "owner")
    monkeypatch.setattr(store, "get_report", lambda *a, **k: {"business_date": "2026-09-24"})
    monkeypatch.setattr(store, "versions", lambda *a, **k: [])
    monkeypatch.setattr(access, "render", lambda *a, **k: {"narrative": {"actions_tomorrow": []}})
    monkeypatch.setattr(models, "get_restaurant", lambda *a, **k: None)
    monkeypatch.setattr(strategy_routes, "_dsr_present_view", lambda *a, **k: presented.append(a))
    u = {"id": 7, "restaurant_id": 1, "role": "client", "is_admin": 0}
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/dsr/2026-09-24?peek=1"):
        body, status = strategy_routes._do_dsr_get(u, "2026-09-24")
    assert status == 200 and presented == []
    with app.test_request_context("/mobile/api/dsr/2026-09-24"):
        strategy_routes._do_dsr_get(u, "2026-09-24")
    assert len(presented) == 1, "opening the report still presents"


def test_the_widget_and_home_card_read_with_peek():
    sync = _read(APP, "Core", "SystemSync.swift")
    assert re.search(r'"/mobile/api/actions", query: \["peek": "1"\]', sync)
    assert re.search(r'"/mobile/api/dsr/\\\(latest\.businessDate\)", query: \["peek": "1"\]', sync)
    card = _read(APP, "Features", "Home", "HomeLastNightCard.swift")
    assert 'query: ["peek": "1"]' in card


# ── F3-16: universal links ───────────────────────────────────────────────────

_AASA_SCRIPT = r'''
import json, os, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()   # pre-create: no legacy adoption
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
aasa = h.apple_app_site_association()
r = h.app.test_client().get("/.well-known/apple-app-site-association")
print("AASA " + json.dumps({"aasa": aasa, "status": r.status_code, "mimetype": r.mimetype,
                            "served": r.get_json(), "app_id": h.APPLE_APP_ID}))
'''


def test_the_apple_app_site_association_names_the_app_and_only_the_dashboard():
    """Runs the real app in a subprocess, as test_home_page_renders does.
    Importing hosted_dashboard into the test process wires CSRF onto
    client_bp for every later test in that worker (csrf_protect runs at
    import), which turned 162 bare-app POSTs into 403s."""
    import json
    vol = tempfile.mkdtemp(prefix="cavnar-aasa-")
    out = subprocess.run([sys.executable, "-c", _AASA_SCRIPT, vol], cwd=ROOT, capture_output=True,
                         text=True, timeout=180)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    got = json.loads(out.stdout.split("AASA ", 1)[1].splitlines()[0])
    aasa = got["aasa"]
    detail = aasa["applinks"]["details"][0]
    assert detail["appIDs"] == ["8DW8XL63K6.ai.cavnar.CavnarAI"]
    assert [c["/"] for c in detail["components"]] == ["/dashboard"]
    assert got["status"] == 200 and got["mimetype"] == "application/json"
    assert got["served"] == aasa
    yml = _read(IOS, "project.yml")
    team = re.search(r"DEVELOPMENT_TEAM: (\w+)", yml).group(1)
    bundle = re.search(r"PRODUCT_BUNDLE_IDENTIFIER: (ai\.cavnar\.CavnarAI)\n", yml).group(1)
    assert got["app_id"] == f"{team}.{bundle}"
    assert re.search(r"com\.apple\.developer\.associated-domains:\n\s+- applinks:dashboard\.cavnar\.ai", yml)


# ── the Swift source rules ───────────────────────────────────────────────────

def test_the_pending_tab_is_consumed_not_just_read():
    root = _read(APP, "RootView.swift")
    block = root.split(".onChange(of: deepLinkRouter.pendingTab)", 1)[1][:200]
    assert "consumePendingTab()" in block


def test_one_carrier_for_where_inside_a_module():
    """F3-7: the section inbox raced the route; the route is the carrier."""
    for path in _swift_files():
        src = _read(path)
        assert "NavSectionInbox" not in src and "onNavSection" not in src, path
    fc = _read(APP, "Features", "FoodCost", "FoodCostQuickEntryView.swift")
    assert "FoodCostAction(path: focus)" in fc
    dest = _read(APP, "Features", "Modules", "ModuleDestinationView.swift")
    assert "FoodCostQuickEntryView(focus: route?.navPath)" in dest


def test_every_location_switch_reaches_root_view():
    store = _read(APP, "Core", "SessionStore.swift")
    body = store.split("func didSwitchLocation", 1)[1].split("\n    }\n", 1)[0]
    assert "onLocationSwitched?(restaurantId)" in body
    root = _read(APP, "RootView.swift")
    assert "session.onLocationSwitched = { _ in didSwitchLocation() }" in root
    reset = root.split("private func didSwitchLocation() {", 1)[1].split("\n    }\n", 1)[0]
    for step in ("modulesPath = NavigationPath()", "homePath = NavigationPath()",
                 "locationSwitches += 1", "refresh(force: true)"):
        assert step in reset, step


def test_signing_out_clears_the_lock_screen_at_once():
    store = _read(APP, "Core", "SessionStore.swift")
    clear = store.split("private func clearLocalSession() {", 1)[1].split("\n    }\n", 1)[0]
    assert "WidgetSnapshotService.clearForSignOut()" in clear


def test_nothing_publishes_on_a_full_swipe():
    for parts in (("Features", "Reviews", "ReviewsListView.swift"),
                  ("Features", "Notifications", "NotificationsListView.swift")):
        src = _read(APP, *parts)
        assert "allowsFullSwipe: true" not in src, parts
    notes = _read(APP, "Features", "Notifications", "NotificationsListView.swift")
    swipe = notes.split(".swipeActions(", 1)[1].split("\n        }\n", 1)[0]
    assert "approve" not in swipe.lower(), "publishing is a tap under the draft, never a swipe"


def test_every_in_app_undo_ends_its_countdown():
    for parts in (("Features", "Notifications", "PendingActionSheet.swift"),
                  ("Features", "Notifications", "NotificationsListView.swift")):
        assert "PendingSendActivities.finish(actionId:" in _read(APP, *parts), parts
    assert "state.offersUndo(isStale: isStale)" in _read(IOS, "CavnarWidgets", "PendingSendLiveActivity.swift")


def test_links_from_outside_never_act():
    root = _read(APP, "RootView.swift")
    assert "deepLinkRouter.openFromLink(nav)" in root
    entry = _read(APP, "Core", "SystemEntry.swift")
    assert "open(fromLink(destination))" in entry


def test_small_fixes_hold():
    hours = _read(APP, "Features", "Account", "AccountHoursSheet.swift")
    assert "MMM d, yyyy" not in hours, "closures read M/D/YY"
    deck = _read(APP, "Features", "Home", "HomeActionDeck.swift")
    assert ".frame(minHeight: height)" in deck and ".frame(height: height)" not in deck
    scan = _read(APP, "Features", "FoodCost", "InvoiceScanSheet.swift")
    assert not re.search(r"\.onAppear \{\s*guard startWithCamera", scan)
    staff = _read(APP, "Features", "Staff", "StaffRequestsViews.swift")
    post = staff.split("private func post(", 1)[1].split("\n    }\n", 1)[0]
    assert "if r.ok {\n                Haptic.success()" in post
    assert "claiming = shift" in staff


def test_the_report_reads_as_the_web_does():
    view = _read(APP, "Features", "DailyReport", "DailyReportView.swift")
    for label in ('"After 6pm"', '"Late clock-ins"', '"Shift quality"', '"Drafts waiting"',
                  '"Replies posted"', '"Running low"'):
        assert label in view, label
    assert '"Late"' not in view
    assert "report.displayedBlocks" in view and "report.withheldLine" in view
    week = _read(APP, "Features", "DailyReport", "DailyReportWeekView.swift")
    assert '"Budget gross"' in week and '"Budget net"' in week and "grid.showsLabor" in week


# ── the S and DB follow-ups (9/25/26) ────────────────────────────────────────

def test_every_approve_reads_whether_google_took_it():
    for parts in (("Push", "PushManager.swift"),
                  ("Features", "Notifications", "NotificationsListView.swift"),
                  ("Features", "Reviews", "ReviewDetailViewModel.swift")):
        src = _read(APP, *parts)
        assert "ReviewPostOutcome" in src, parts
        assert "auto_posted" not in src, f"{parts}: post_status is read through ReviewPostOutcome"
    review = _read(APP, "Models", "Review.swift")
    assert '"post_status"' in review and '"post_note"' in review


def test_the_publish_names_its_week_and_the_keys_it_acknowledges():
    sheet = _read(APP, "Features", "Labor", "PublishScheduleSheet.swift")
    assert "guard let scheduleId else {" in sheet
    assert "try c.encode(keys, forKey: .acknowledge)" in sheet
    assert '"blocker_items"' in sheet and '"unsent_changes"' in _read(APP, "Features", "Labor", "LaborViewModel.swift")
    assert '"schedule_changes_send"' in _read(IOS, "Shared", "PendingSendActivity.swift")


def test_an_open_pos_day_is_asked_about_then_closed_early():
    vm = _read(APP, "Features", "DailyReport", "DailyReportViewModel.swift")
    assert vm.count("DSRCloseGate.isBeforeClose(error)") == 2
    assert "closeDay(early: true)" in _read(APP, "Features", "DailyReport", "DailyReportView.swift")
    assert "closeTonight(early: true)" in _read(APP, "Features", "DailyReport", "DailyReportListView.swift")
