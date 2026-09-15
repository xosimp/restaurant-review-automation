"""The web Account panel's contract with the JavaScript that drives it.

The panel was rebuilt from scratch (Sep 2026) into a restaurant command
center: overview header, account health, a sticky section rail, and one
component system. Every function that reads or writes it does so by element
id, so the real risk in any future restructure is a renamed or dropped id
that makes a loader silently no-op — "Loading…" forever, no error anywhere.
This pins that contract, plus the specific defects the rebuild removed so
they cannot quietly come back.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _panel():
    s = _src()
    i = s.index('id="panel-account"')
    return s[i:s.index('id="toast"', i)]


def _scripts():
    return "\n".join(re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", _src(), re.DOTALL))


# Every id an existing loader, saver, or handler reaches for. Extracted from
# the JS at the time of the rebuild; if you rename one of these on purpose,
# rename it in the JS in the same commit and update this list.
CONTRACT = """
panel-account acct-status-dot profile-edit-btn profile-email-display profile-edit
pe-owner-name pe-phone pe-tz pe-tone pe-lang pe-signoff pe-voice pe-never pe-menu pe-save
acct-email-display dark-mode-toggle-input dark-mode-track dark-mode-thumb
revoke-sessions-btn sessions-list sec-score sec-bar sec-items bc-remaining bc-codes trusted-list
rec-status rec-email rec-verify rec-code login-history activity-log
alerts-contacts-summary alerts-types-summary
billing-loading billing-content billing-next-prominent billing-amount-prominent billing-status
billing-pm billing-portal-link billing-invoice-link billing-no-sub
acct-team-list team-name team-email bug-msg
staff-join-code staff-portal-url staff-list staff-unclaimed staff-new-name staff-new-role
staff-role-options staff-new-pin staff-pin-events
referral-name referral-email referral-note referral-status
account-settings-card as-auto-enabled as-auto-cap as-auto-paused as-auto-status as-hours-grid
as-closures as-hours-status as-retention as-login-notify as-marketing as-data-status eh-body
webhook-config wh-disabled-banner wh-disabled-reason webhook-secret wh-copy-btn
webhook-last-status wh-history-arrow wh-history-panel wh-history-body webhook-url
evt-count-label evt-arrow evt-panel whe-review whe-negative whe-positive whe-alert whe-approved
whe-posted whe-labor-over whe-labor-updated whe-inv-updated whe-inv-cost whe-fc-spike
whe-fc-trend whe-intel whe-report wh-save-btn wh-test-btn wh-delete-btn wh-status
""".split()


@pytest.mark.parametrize("el_id", CONTRACT)
def test_every_id_the_account_js_depends_on_is_in_the_panel(el_id):
    assert 'id="%s"' % el_id in _panel(), el_id


def test_the_page_opens_on_the_restaurant_not_the_consultant():
    """The old hero was Will's photo with the restaurant name as a caption.
    The first heading in the panel is now the restaurant."""
    p = _panel()
    first_h1 = re.search(r"<h1[^>]*>", p)
    assert first_h1 and 'class="ac-name"' in first_h1.group(0)
    assert "restaurant.brand_name or restaurant.name" in p[first_h1.start():first_h1.start() + 200]


def test_sign_in_notifications_have_exactly_one_control():
    """Two controls used to write the same flag through two endpoints and
    never agreed with each other on screen."""
    p, s = _panel(), _src()
    assert p.count('id="as-login-notify"') == 1
    for gone in ("login-notify-track", "login-notify-thumb", "login-notify-label"):
        assert gone not in p, gone
    assert "toggleLoginNotify" not in s


def test_opening_account_does_not_fetch_other_panels_data():
    """_acctExtras used to call the Intel and Labor loaders — a wasted
    /api/ai-visibility/history request on every Account open."""
    m = re.search(r"window\._acctExtras=function\(\)\{[^\n]*", _scripts())
    assert m, "_acctExtras definition not found"
    assert "loadAivHistory" not in m.group(0) and "loadScheduleHistory" not in m.group(0)


def test_acct_extras_exists_before_the_tab_can_be_opened():
    """It used to be defined inside a DOMContentLoaded handler registered
    late in the file, while the hash-restore handler that opens the tab is
    registered earlier and fires first — so on a direct link to #account the
    loaders never ran. Definition must sit at IIFE top level (2-space indent),
    not inside a handler (4+)."""
    line = [ln for ln in _scripts().split("\n") if "window._acctExtras=function()" in ln]
    assert len(line) == 1
    assert line[0].startswith("  window._acctExtras=") and not line[0].startswith("   "), line[0][:60]


def test_the_section_rail_and_the_sections_agree():
    p = _panel()
    rail = set(re.findall(r'data-go="([a-z]+)"', p))
    sections = set(re.findall(r'class="ac-section" id="acct-([a-z]+)"', p))
    assert rail == sections, rail ^ sections


def test_account_health_covers_the_six_areas():
    assert set(re.findall(r'data-health="([a-z]+)"', _panel())) == {
        "profile", "people", "integrations", "notifications", "security", "subscription"}


def test_no_hard_coded_hex_colours_in_the_panel():
    """The old panel hard-coded #4ade80/#ef9f27/#22c55e/#d0cbc4/… and needed a
    twenty-line :where() hack to undo them in light mode. Tokens only now."""
    p = _panel()
    hits = re.findall(r"#[0-9a-fA-F]{6}\b", p)
    assert not hits, hits[:8]


def test_toggles_are_real_checkboxes_not_divs_with_onclick():
    p = _panel()
    assert re.search(r'<div[^>]*onclick="toggle', p) is None
    switches = re.findall(r'<label class="ac-switch">(.*?)</label>', p, re.DOTALL)
    assert switches and all('type="checkbox"' in sw for sw in switches)


def test_every_disclosure_declares_its_state():
    p = _panel()
    for tag in re.findall(r"<button[^>]*aria-controls=[^>]*>", p):
        assert 'aria-expanded="' in tag, tag[:100]


def test_pos_connections_come_from_one_macro():
    s = _src()
    assert s.count("{% macro pos_card(") == 1
    assert s.count("{{ pos_card(") == 3


def test_dark_mode_switch_is_styled_by_css_not_inline_js_colours():
    s = _scripts()
    i = s.index("function toggleDarkMode()")
    body = s[i:s.index("\n}\n", i)]
    assert "#" not in body.replace("'#'+", ""), "toggleDarkMode still writes hex colours inline"


def test_revoking_sessions_uses_the_button_system_not_inline_backgrounds():
    s = _scripts()
    i = s.index("function revokeOtherSessions(btn)")
    body = s[i:s.index("\n}\n", i)]
    assert "style.background" not in body and "cbtnBusy(" in body


def test_stale_helper_classes_are_gone():
    """`.acct-card-title` / `.acct-card-sub` were used on two cards and
    defined nowhere, so those titles rendered unstyled."""
    assert "acct-card-title" not in _src() and "acct-card-sub" not in _src()
