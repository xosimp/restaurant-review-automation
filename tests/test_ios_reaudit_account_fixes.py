"""iOS blind re-audit, Account / Team / People fix round (10/8/26).

The server halves of the findings, and source-level pins for the rules the
phone fixes protect:

  * A2   the phone's contact save sends only what changed, and a field not
         sent (the reply language) is never cleared by the route.
  * M15  the owner's phone is an account holder's to change, compared by
         its digits so a teammate's unchanged form still saves.
  * L2   the account summary says whether a 2FA code can reach THIS login
         by text.
  * M1/M2 the web routes the app's Schedule history and Email history rows.
  * A3, A5, A6, M11, M12 — pinned in the Swift source.
"""
import os
import re

import pytest

import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Account")


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _account(name):
    with open(os.path.join(ACCOUNT, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def phone(monkeypatch, db_path):
    """A mobile client with an owner and a manager login on one restaurant."""
    from flask import Flask
    import auth, auth_routes, client_api, mobile_api
    from auth import create_user
    real = models.get_conn
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    for mod in (auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    auth.init_auth(db_path=db_path)
    auth_routes._login_attempts.clear()
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    client = app.test_client()
    rid = create_restaurant(Restaurant(name="Phone Co", owner_email="owner1@x.test"))
    models.update_restaurant(rid, {"owner_name": "Erik", "owner_phone": "3125550100",
                                   "response_language": "es", "timezone": "America/Denver"})
    create_user(rid, "owner1", "owner1@x.test", "correct-horse", db_path=db_path)
    create_user(rid, "mgr1", "mgr1@x.test", "correct-horse", db_path=db_path, role="manager")

    def tok(u):
        r = client.post("/mobile/api/login", json={"username": u, "password": "correct-horse"})
        return {"Authorization": "Bearer " + r.get_json()["token"]}
    return client, rid, tok("owner1"), tok("mgr1")


def test_a_contact_save_never_clears_the_reply_language(phone):
    client, rid, own, _ = phone
    r = client.post("/mobile/api/account/update-profile", headers=own, json={"owner_name": "Erik B"})
    assert r.status_code == 200
    got = models.get_restaurant(rid)
    assert got.owner_name == "Erik B"
    assert got.response_language == "es" and got.timezone == "America/Denver"   # not sent: kept


def test_the_owners_phone_is_the_owners(phone):
    client, rid, own, mgr = phone
    # The web form sends the phone as shown: the same digits are no change.
    r = client.post("/mobile/api/account/update-profile", headers=mgr,
                    json={"owner_phone": "(312) 555-0100", "owner_name": "Erik"})
    assert r.status_code == 200
    r = client.post("/mobile/api/account/update-profile", headers=mgr, json={"owner_phone": "3125559999"})
    assert r.status_code == 403 and r.get_json()["owner_only"]
    assert "".join(c for c in models.get_restaurant(rid).owner_phone if c.isdigit()) == "3125550100"
    r = client.post("/mobile/api/account/update-profile", headers=own, json={"owner_phone": "3125559999"})
    assert r.status_code == 200
    assert "".join(c for c in models.get_restaurant(rid).owner_phone if c.isdigit()) == "3125559999"


def test_the_summary_says_whether_this_login_can_get_a_text_code(phone):
    client, _, own, _ = phone
    body = client.get("/mobile/api/account", headers=own).get_json()
    assert "two_fa_sms_available" in body["account"]
    assert body["account"]["two_fa_sms_available"] in (True, False, None)


def test_the_phone_sends_only_the_changed_contact_fields():
    vm = _account("AccountViewModel.swift")
    body = vm[vm.index("private struct UpdateProfileBody"):vm.index("func updateProfile(")]
    # Optional, so a nil field is left out of the JSON.
    for field in ("ownerName", "ownerPhone", "voiceNotes", "neverSay", "menuNotes", "timezone",
                  "signOffName", "responseLanguage"):
        assert re.search(r"var %s: String\? = nil" % field, body), field
    sheet = _account("AccountProfileDetailView.swift")
    assert "ownerName: nameChanged ? ownerName : nil" in sheet
    assert "voiceNotes:" not in sheet[sheet.index("private func saveContact"):]


def test_the_web_routes_the_apps_history_rows():
    dash = _src("templates", "dashboard.html")
    assert 'data-stage="history" data-nav="labor/history"' in dash
    assert "if (p.rest[0] === 'email-history')" in dash
    assert "window.acctFold(ehBtn, 'eh-fold')" in dash
    view = _account("AccountView.swift")
    assert 'webRow("Schedule history", path: "labor/history")' in view
    assert 'webRow("Email history", path: "account/email-history")' in view


def test_owner_only_links_and_fixes_stay_the_owners():
    view = _account("AccountView.swift")
    assert "case .team: showingTeam = isOwner" in view
    assert "case .staff: showingStaff = isOwner" in view
    assert 'case "staff": self = .staff' in view
    assert 'case .export: openWeb("account/data")' in view
    # Close my account and Delete my login stay in the app (5.1.1(v)).
    assert 'row("Close my account"' in view and 'row("Delete my login"' in view


def test_failures_are_said_where_they_happen():
    vm = _account("AccountViewModel.swift")
    assert "case inviteEmailSent = \"invite_email_sent\"" in vm
    assert "private func disconnect(_ path: String, name: String) async -> Bool" in vm
    assert "// Low-stakes background action" not in vm
    conns = _account("AccountConnectionsDetailView.swift")
    assert "if await pending.action() { Haptic.success() } else { Haptic.error() }" in conns


def test_turning_off_two_factor_is_asked_first():
    sec = _account("AccountSecurityDetailView.swift")
    assert 'confirmationDialog("Turn off two-factor?"' in sec
    assert "if on { showing2FASetup = true } else { confirmingDisable2FA = true }" in sec


def test_kit_rows_are_whole_row_targets_with_named_switches():
    kit = _account("AccountSheetKit.swift")
    action = kit[kit.index("struct AccountActionRow"):]
    assert action.index("Button {") < action.index("HStack(alignment: .center")   # the row is the button
    switch_row = kit[kit.index("struct AccountSwitchRow"):kit.index("struct AccountActionRow")]
    assert "accessibilityName: label" in switch_row and ".onTapGesture { rowTaps += 1 }" in switch_row
