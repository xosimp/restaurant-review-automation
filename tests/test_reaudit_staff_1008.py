"""iOS blind re-audit, Staff app (10/8/26): the rules the fixes protect,
read from the Swift source (no simulator here).

H1  leaving Requests inside the Undo window SENDS the withdraw it promised
H2  notifications denied in iPhone Settings → the row opens Settings
M3  every staff sheet with a form guards unsaved edits (Discard changes?)
M5  the post-shift pulse is on Today only, and a send clears the store's
L16 the PIN dots go red only for a refused PIN
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAFF = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Staff")


def _read(name):
    with open(os.path.join(STAFF, name), encoding="utf-8") as f:
        return f.read()


def test_leaving_requests_inside_the_undo_window_sends_the_withdraw():
    src = _read("StaffRequestsViews.swift")
    assert ".onDisappear { commitPendingNow() }" in src
    assert ".onDisappear { cancelUndo() }" not in src
    body = src.split("private func commitPendingNow()", 1)[1].split("\n    }\n", 1)[0]
    assert "commit(p)" in body, "the pending withdraw is sent, never dropped"


def test_a_denied_notification_permission_opens_settings():
    src = _read("StaffMeView.swift")
    assert "UIApplication.openSettingsURLString" in src
    assert "PushManager.shared.authorizationDenied" in src
    assert "Off in iPhone Settings" in src


def test_every_staff_form_sheet_guards_unsaved_edits():
    for name, title in [("StaffSelfServiceViews.swift", '"My availability"'),
                        ("StaffSelfServiceViews.swift", '"What I\\u{2019}d like"'),
                        ("StaffRequestsViews.swift", '"Time off"'),
                        ("StaffRequestsViews.swift", '"Swap shift" : "Give up shift"'),
                        ("StaffTodayCards.swift", '"Running late"'),
                        ("StaffInboxViews.swift", '"Your manager"')]:
        src = _read(name)
        assert re.search(r"accountSheetChrome\([^\n]*?" + re.escape(title) + r",\s*isDirty:", src), (name, title)


def test_the_pulse_lives_on_today_and_a_send_clears_it_everywhere():
    inbox = _read("StaffInboxViews.swift")
    assert "StaffPulseCard(store: store)" not in inbox.split("struct StaffMessageThreadView", 1)[0]
    assert inbox.count("portal?.pulseAnswered()") == 2, "a send and an already-answered 409 both clear it"
    store = _read("StaffPortalStore.swift")
    assert "var pulseDue: StaffPulseDue?" in store and "self.reloadPulse()" in store
    assert "StaffPulseCard(store: store)" in _read("StaffTodayView.swift")


def test_the_pin_dots_turn_red_only_for_a_refused_pin():
    login = _read("StaffLoginView.swift")
    assert "isError: error != nil" not in login
    assert "pinRefused = staff.lastErrorWasPin" in login
