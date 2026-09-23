"""Web accessibility and syntax-compatibility edge cases (CLIENT audit).

tests/test_frontend_rules.py enforces ES5 on dashboard.html only, and its
banned list stops at const/let/arrows/backticks/async — so optional
chaining, `??`, spread, `class` and `for…of` would pass even there, and no
staff or sign-in page is checked at all (CLIENT-44). Those pages are the
ones reached from a staff member's own, possibly old, phone. They are clean
today; these tests keep them that way.

Accessibility (CLIENT-59 and the audit's a11y rows):
- pinch-zoom must never be disabled on any page;
- a `:focus-visible` rule that removes the outline must put a visible
  indicator in its place — dashboard.html removes it outright for every
  input, textarea and select;
- a clickable `<div>` must be reachable by keyboard (`role` + `tabindex`):
  the reply-template rows and the "new reviews" banner are mouse-only.
"""
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "templates")

# Every page a non-admin reaches: staff, sign-in, public, and the owner's
# dashboard. Admin-only pages (admin, audit_*, client_*) are covered by
# tests/test_edge_client_web_admin.py.
NON_ADMIN_PAGES = [
    "dashboard.html", "_review_card.html", "_csrf_fetch.html",
    "staff_login.html", "staff_portal.html", "staff_schedule.html",
    "staff_schedule_expired.html", "staff_schedule_invalid.html",
    "login.html", "forgot_password.html", "reset_password.html", "two_fa.html",
    "admin_two_factor.html", "status.html", "guest_optin.html", "unsubscribed.html",
    "billing_paused.html", "issue.html", "rate_limited.html",
]
STATIC_JS = sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, "static", "*.js")))

ES5_EXTRA = [
    (r"\?\.(?=[A-Za-z_$\[(])", "optional chaining ?."),
    (r"\?\?(?!=)", "nullish coalescing ??"),
    (r"\.\.\.(?=[A-Za-z_$\[(])", "spread / rest ..."),
    (r"\bclass\s+[A-Za-z_$][\w$]*\s*(?:extends\b|\{)", "class declaration"),
    (r"\bfor\s*\(\s*(?:var\s+)?[\w$]+\s+of\s", "for...of"),
]
ES2015_BASE = [
    (r"`", "backtick template literal"),
    (r"\bconst\s", "const declaration"),
    (r"\blet\s", "let declaration"),
    (r"=>", "arrow function"),
    (r"\basync\s+function\b", "async function"),
    (r"\bawait\s", "await"),
]


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _js(rel):
    text = _read(rel)
    if rel.endswith(".js"):
        return text
    return "\n".join(re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", text, re.S))


def _strip_strings_and_comments(js):
    """Blank out string literals and comments so prose like 'Read more...'
    or "e.g. ?? here" in copy can't trip a syntax scan."""
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    js = re.sub(r"(?<![:\\])//[^\n]*", " ", js)
    return re.sub(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", "''", js)


def _offenders(rel, pattern):
    return [ln.strip()[:100] for ln in _strip_strings_and_comments(_js(rel)).split("\n") if re.search(pattern, ln)]


def test_the_page_lists_are_real_files():
    for name in NON_ADMIN_PAGES:
        assert os.path.exists(os.path.join(T, name)), name
    assert STATIC_JS


@pytest.mark.parametrize("pattern,label", ES5_EXTRA, ids=[lbl for _p, lbl in ES5_EXTRA])
@pytest.mark.parametrize("rel", ["templates/" + p for p in NON_ADMIN_PAGES] + STATIC_JS)
def test_pages_staff_and_owners_reach_avoid_post_es5_syntax(rel, pattern, label):
    hits = _offenders(rel, pattern)
    assert not hits, "%s in %s:\n%s" % (label, rel, "\n".join(hits[:5]))


@pytest.mark.parametrize("pattern,label", ES2015_BASE, ids=[lbl for _p, lbl in ES2015_BASE])
@pytest.mark.parametrize("rel", ["templates/" + p for p in NON_ADMIN_PAGES if p != "dashboard.html"] + STATIC_JS)
def test_staff_and_sign_in_pages_are_es5_like_the_dashboard(rel, pattern, label):
    """test_frontend_rules.py covers dashboard.html; this is the same rule
    for every other page a non-admin loads."""
    hits = _offenders(rel, pattern)
    assert not hits, "%s in %s:\n%s" % (label, rel, "\n".join(hits[:5]))


# ── zoom ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(os.path.basename(p) for p in glob.glob(os.path.join(T, "*.html"))))
def test_no_page_disables_pinch_zoom(name):
    for content in re.findall(r'<meta\s+name="viewport"\s+content="([^"]*)"', _read("templates/" + name)):
        c = content.replace(" ", "").lower()
        assert "user-scalable=no" not in c and "user-scalable=0" not in c, name
        cap = re.search(r"maximum-scale=([\d.]+)", c)
        assert cap is None or float(cap.group(1)) >= 2, name


# ── focus rings ─────────────────────────────────────────────────────────────

def _css(text):
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))


def _focus_visible_rules(css):
    for m in re.finditer(r"([^{}]*:focus-visible[^{}]*)\{([^{}]*)\}", css):
        yield m.group(1).strip(), m.group(2).replace(" ", "")


def test_the_dashboard_has_focus_visible_rules():
    assert list(_focus_visible_rules(_css(_read("templates/dashboard.html"))))


def test_a_focus_visible_rule_never_removes_the_ring_without_a_replacement():
    bad = []
    for sel, decl in _focus_visible_rules(_css(_read("templates/dashboard.html"))):
        removes = re.search(r"outline:(none|0)(?:[;!]|$)", decl)
        replaced = re.search(r"box-shadow:(?!none)|border-color:|outline-color:|text-decoration:underline", decl)
        if removes and not replaced:
            bad.append(sel[-80:])
    assert not bad, bad


# ── keyboard-reachable click targets ────────────────────────────────────────

# The two targets the audit found mouse-only. Modal backdrops (click outside
# to close) and the Labor subsection rows (which contain real buttons) are
# deliberately not in this list. Turning either into a <button> makes the
# pattern stop matching, which passes the test (and flips the xfail).
MOUSE_ONLY_TARGETS = {
    "new reviews banner": r'<div id="new-reviews-banner"[^>]*>',
    "reply template row": r"'<div onclick=\"insertTemplate\([^>]*>",
}


@pytest.mark.parametrize("target", sorted(MOUSE_ONLY_TARGETS))
def test_a_clickable_div_is_reachable_by_keyboard(target):
    page = _read("templates/dashboard.html")
    for tag in re.findall(MOUSE_ONLY_TARGETS[target], page):
        assert "role=" in tag and "tabindex=" in tag, tag[:120]
