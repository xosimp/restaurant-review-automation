"""The button design system (static/css/cavnar-buttons.css).

Every button in the client web app is built from one component, .cbtn, plus
variants and sizes. The six legacy families that used to compete in
dashboard.html (.btn/.btn-approve/.btn-skip, .btn-primary, .btn-secondary,
.hb-btn/.hb-link, .btn-blue, .btn-link-*) — along with the dark-mode
!important overrides that forced primaries to a muddy #923020, the
per-module re-skins (#panel-reviews .btn, .lb2-actions .btn-primary,
.fc2-submit .btn-primary) and the universal `button:not(...)` hover hack —
are gone, and nothing may quietly bring them back.

Chips, tabs, filter pills, segmented range toggles, list-row selectors and
header chrome are deliberately not buttons in this sense; they're the
allow-list below. Anything else must carry .cbtn.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "css", "cavnar-buttons.css")
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")
STANDALONE = ["login.html", "two_fa.html", "forgot_password.html", "reset_password.html", "guest_optin.html", "staff_schedule.html"]

LEGACY = re.compile(r"(^|[^c-])btn-(primary|secondary|approve|skip|approved|blue|link-)|(^|[^-])hb-btn\b|(^|[^-])hb-link\b")

# Interactive elements that are intentionally not system buttons.
EXEMPT_CLASSES = {"tab", "rm", "hb-refresh", "hb-chip", "hb-x", "hb-loc", "hm-days-btn", "fpill", "rv2-x",
                  "ask-chip", "ask-send", "faq-q", "rv2-topic", "more", "on", "rv-tab-active", "mtab",
                  "ac-hitem",  # Account health items: list-row selectors that jump to a section
                  "in2-viewall", "in2-gbp-link"}  # AI visibility: text-style "view all" / checklist nav links
EXEMPT_IDS = {"loc-switcher-btn", "notif-btn", "changelog-btn", "team-msg-btn", "mkt-tab-content-btn",
              "mkt-tab-queue-btn", "mkt-tab-analytics-btn", "sched-toggle-label", "perf-tab-dow",
              "perf-tab-trend", "ask-cavnar-fab"}
EXEMPT_ATTRS = ("data-ask=", "data-score=", "data-quality-shift=", 'role="listitem"', 'role="tab"')
EXEMPT_ONCLICK = ("switchLocation(", "hbRange(", "hbAct(", "askSuggest(", "askOpenConversation(")


def _src(path=DASHBOARD):
    return open(path, encoding="utf-8").read()


def _open_tags(text, name="button"):
    """Every <name ...> open tag, quote-aware (onclick values contain '>')."""
    out, i = [], 0
    while True:
        i = text.find("<" + name, i)
        if i < 0:
            return out
        j, q = i, None
        while j < len(text):
            c = text[j]
            if q:
                if c == q: q = None
            elif c in "\"'": q = c
            elif c == ">": break
            j += 1
        out.append((text.count("\n", 0, i) + 1, text[i:j + 1]))
        i = j + 1


def test_the_stylesheet_defines_every_variant_size_and_state():
    css = _src(CSS)
    for sel in (".cbtn{", ".cbtn-primary{", ".cbtn-secondary", ".cbtn-text{", ".cbtn-soft{", ".cbtn-danger{",
                ".cbtn-danger.cbtn-ghost{", ".cbtn-success{", ".cbtn-glass{", ".cbtn-icon{", ".cbtn-sm{",
                ".cbtn-lg{", ".cbtn-block{", ".cbtn-inline{", ".cbtn-close{", ".cbtn-row{",
                ".cbtn:hover{", ".cbtn:active{", ".cbtn:focus-visible{", ".cbtn:disabled", '.cbtn[aria-busy="true"]{',
                "prefers-reduced-motion"):
        assert sel in css, "missing " + sel


def test_dark_mode_primary_is_the_ios_dark_ember_not_the_old_muddy_override():
    css = _src(CSS)
    dark = css[css.index('[data-theme="dark"]{'):]
    assert "--cb-accent:#d4583a" in dark
    rules_only = re.sub(r"/\*.*?\*/", "", _src(CSS), flags=re.S)
    assert "#923020" not in rules_only
    assert '[data-theme="dark"] .btn-primary' not in _src()


def test_no_legacy_button_family_survives_in_the_dashboard():
    hits = [(n + 1, ln[:120]) for n, ln in enumerate(_src().split("\n")) if LEGACY.search(ln)]
    assert not hits, hits[:8]


def test_the_universal_button_hover_hack_and_per_module_reskins_are_gone():
    s = _src()
    assert "button:not(.tab):not(.logout-btn)" not in s
    assert "#panel-reviews .btn" not in s
    assert ".lb2-actions .btn-primary" not in s and ".fc2-submit .btn-primary" not in s


def test_every_dashboard_button_is_a_system_button_or_a_listed_exception():
    s = _src()
    strays = []
    for line, tag in _open_tags(s):
        if "cbtn" in tag:
            continue
        cls = re.search(r'class="([^"]*)"', tag)
        classes = set(cls.group(1).replace("'", " ").split()) if cls else set()
        idm = re.search(r'id="([^"]*)"', tag)
        if classes & EXEMPT_CLASSES or (idm and idm.group(1) in EXEMPT_IDS):
            continue
        if any(a in tag for a in EXEMPT_ATTRS) or any(o in tag for o in EXEMPT_ONCLICK):
            continue
        strays.append((line, tag[:110]))
    assert not strays, "buttons outside the system (add .cbtn or list them as an exception): %r" % strays[:10]


def test_system_buttons_carry_no_inline_presentation_overrides():
    """Layout-only inline styles (display:none, margins, flex-shrink, width)
    are fine; sizing/colour/typography must come from the variant classes."""
    bad = []
    for line, tag in _open_tags(_src()):
        if "cbtn" not in tag:
            continue
        st = re.search(r'style="([^"]*)"', tag)
        if not st:
            continue
        for decl in st.group(1).split(";"):
            prop = decl.split(":")[0].strip()
            if prop in ("font-size", "padding", "background", "background-color", "color", "border",
                        "border-radius", "font-weight", "font-family", "height", "box-shadow"):
                bad.append((line, decl.strip()))
    assert not bad, bad[:10]


def test_the_two_js_selectors_that_targeted_legacy_classes_were_updated_with_the_markup():
    s = _src()
    # The Approve button itself moved from .cbtn-success (green) to
    # .cbtn-primary (ember) — same color as approving is meant to look
    # like the one dominant action, not indistinguishable from the green
    # "done" state it turns into — so this press-animation selector had to
    # move with it.
    assert "document.querySelector('#rc-'+id+' .cbtn-primary')" in s
    assert "document.querySelector('.cbtn-primary[onclick*=\"genContent\"]')" in s
    assert "document.querySelector('#twofa-step2 .cbtn-primary')" in s
    assert "confirm.className = 'cbtn cbtn-primary cbtn-sm';" in s


def test_busy_state_uses_the_orb_and_blocks_double_submits():
    s = _src()
    i = s.index("function cbtnBusy(btn, label) {")
    body = s[i:s.index("\n}\n", i)]
    assert "btn.setAttribute('aria-busy', 'true')" in body and "btn.disabled = true" in body
    assert 'class="hb-orb"' in body and 'data-orb-state="working"' in body
    assert "function busy(btn,label){return cbtnBusy(btn,label||'Working…');}" in s


@pytest.mark.parametrize("name", STANDALONE)
def test_standalone_pages_use_the_shared_sheet_and_own_no_button_rules(name):
    t = _src(os.path.join(ROOT, "templates", name))
    assert '<link rel="stylesheet" href="/static/css/cavnar-buttons.css">' in t
    assert 'class="cbtn cbtn-primary cbtn-lg cbtn-block"' in t
    assert not re.search(r"\n\s*\.btn[a-z-]*\s*[{:]|\.avail button\s*\{", t), name + " still defines its own button rules"
