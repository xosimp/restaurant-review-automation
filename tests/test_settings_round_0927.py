"""Settings round, 9/27/26: the two-factor fix lands on Security and opens
the setup, popovers close from outside, the profile's menu field is gone and
its confirm can't be pressed twice, issue routing can add someone to text,
one frequency dial, the auto-approve Saved state, What you've decided gone,
Data health pills grouped by colour, and the logs open as modals."""
import pytest
from flask import Flask

import auth
import models
from models import Restaurant, create_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


# ── the overview ────────────────────────────────────────────────────────────

def test_turn_on_two_factor_opens_the_setup_on_security():
    fix = _between("var FIX={", "var FIX_ORDER=")
    assert "security:['Turn on two-factor',function(){acctGo('security');" in fix
    assert "cModal.open('twofa-modal')" in fix
    assert 'class="cbtn cbtn-secondary cbtn-sm" data-twofa-enable>Enable</button>' in SRC
    go = _between("window.acctGo=function(section,focusId){", "function setCurrent(section){")
    # A scroll aimed before the sections above grew stopped on Integrations.
    assert "[700,1500].forEach(" in go and "if(Math.abs(off)>24)el.scrollIntoView(" in go


def test_popovers_close_from_anywhere_outside():
    assert "var open=document.querySelectorAll('details.ac-more[open]');if(!open.length" in SRC
    feed = _between("window.aiFeedToggle = function () {", "  load();\n  pollTimer")
    assert "if (e.target.closest('#ai-feed') || e.target.closest('#ai-strip')) return;" in feed


# ── profile ─────────────────────────────────────────────────────────────────

def test_the_profile_has_no_menu_field_and_keeps_the_notes():
    assert 'id="pe-menu"' not in SRC and "Menu highlights" not in SRC
    save = _between("window.saveProfile=function(){", "\n  };")
    # Sending an empty menu_notes would have wiped what Marketing's brand
    # voice keeps there.
    assert "menu_notes" not in save and "pe-menu" not in save
    assert 'id="bv-menu"' in SRC


def test_confirm_profile_is_one_press_with_a_green_toast():
    fn = _between("function saveRestaurantProfile() {", "\n}\n")
    assert "if (btn.disabled) return; btn.disabled = true;" in fn
    assert "toast('Profile confirmed', 'success')" in fn and "RP_SAVED = rpSnap(); rpSync();" in fn
    assert "btn.classList.toggle('cbtn-success', !!saved)" in _between("function acSavedState(", "\n}\n")
    assert "RP_SAVED = p.confirmed ? rpSnap() : null; rpSync();" in SRC


def test_auto_approve_rests_in_a_green_saved_state():
    assert 'id="as-auto-save" onclick="saveAutoApprove()"' in SRC
    sync = _between("window.aaSync = function() {", "\n};")
    assert "acSavedState(btn, same && on, 'Saved', 'Save rule');" in sync
    assert "if(window.aaSync)aaSync();" in _between("window.acctCapMeter=function(){", "var cap=")


# ── notifications ───────────────────────────────────────────────────────────

def test_one_frequency_dial():
    alerts = _between('id="alerts-modal"', "saveAlertSettings()\" class=")
    assert 'id="al-max-per-day"' not in alerts and "Do-not-disturb &amp; frequency" not in alerts
    assert "al-max-per-day" not in SRC
    assert 'id="as-level-card"' in SRC


def test_no_literal_unicode_escape_renders_as_text():
    assert "tonight\\u2019s lineup" not in SRC and "can\\u2019t be acted on" not in SRC


def test_issue_routing_offers_to_add_someone():
    paint = _between("function paintIssueRouting(d) {", "\nfunction loadIssueRouting()")
    assert "'<option value=\"__add\">+ Add someone to text\\u2026</option>'" in paint
    assert 'id="as-route-add"' in SRC and 'id="as-route-add-consent"' in SRC
    save = _between("function routeAddSave(btn) {", "\n}\n")
    assert "'/api/issues/routing/contact'" in save and "consent: true" in save


@pytest.fixture
def client(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    rid = create_restaurant(Restaurant(name="Route Co", owner_email="r@x.test"), db_path=db_path)
    return app.test_client(), rid


def _as(monkeypatch, rid, role="client"):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 5, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.test"})


def test_adding_someone_routes_issues_to_them(client, monkeypatch):
    import issues
    import notify
    c, rid = client
    _as(monkeypatch, rid)
    r = c.post("/api/issues/routing/contact", json={"role": "manager", "name": "Dana", "phone": "(312) 555-0142"})
    assert r.status_code == 400 and "agreed" in r.get_json()["error"]            # no consent, nothing added
    assert notify.get_alert_contacts(rid) == []
    got = c.post("/api/issues/routing/contact",
                 json={"role": "manager", "name": "Dana", "phone": "(312) 555-0142", "consent": True}).get_json()
    assert got["ok"] and [x["name"] for x in got["contacts"]] == ["Dana"] and got["contacts"][0]["sms_consent"]
    assert issues.get_routing(rid)["manager"]["name"] == "Dana"
    # The same number again routes escalation to the same person, no second row.
    got = c.post("/api/issues/routing/contact",
                 json={"role": "escalation", "name": "Dana", "phone": "312-555-0142", "consent": True}).get_json()
    assert len(got["contacts"]) == 1 and issues.get_routing(rid)["escalation"]["name"] == "Dana"
    # At most two people are texted.
    c.post("/api/issues/routing/contact", json={"role": "escalation", "name": "Sam", "phone": "3125550199", "consent": True})
    r = c.post("/api/issues/routing/contact", json={"role": "manager", "name": "Lee", "phone": "3125550111", "consent": True})
    assert r.status_code == 400 and "at most two" in r.get_json()["error"]
    # A teammate cannot.
    _as(monkeypatch, rid, "manager")
    assert c.post("/api/issues/routing/contact", json={"role": "manager", "name": "X", "phone": "3125550100",
                                                        "consent": True}).status_code == 403


# ── automation, data health, security ───────────────────────────────────────

def test_what_youve_decided_left_settings():
    assert 'id="acct-decisions-card"' not in SRC and "function loadDecisions()" not in SRC
    routes = open("strategy_routes.py", encoding="utf-8").read()
    assert '("/decisions", ["GET"], _do_decisions, "decisions")' in routes      # Ask and the phone still read it


def test_data_health_pills_group_by_colour():
    fn = _between("window.acctApplyDataHealth=function(d){", "\n  };")
    assert "var rank=hc==='zero'?2:" in fn and "pills.sort(function(a,b){return a.r-b.r||a.i-b.i;});" in fn


def test_the_logs_open_as_modals():
    assert "onclick=\"acctLogOpen('login-history','Sign-in history')\"" in SRC
    assert "onclick=\"acctLogOpen('activity-log','Account activity')\"" in SRC
    op = _between("window.acctLogOpen=function(id,title){", "\n  };")
    assert "m.className='cmodal so-modal ac-log-modal'" in op and "cModal.open('acct-log-modal',{close:'acctLogClose'})" in op
    assert "acctLogHome();m.style.display='none';" in SRC
