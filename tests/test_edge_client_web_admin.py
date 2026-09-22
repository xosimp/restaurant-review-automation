"""The admin console's client plumbing (templates/admin.html).

The admin console is one hash-routed page with a single `api()` helper that
every call goes through. What the CLIENT audit asked of it:

- a 401 sends the admin back to sign-in (it does — pinned here);
- a non-JSON 502 from Railway's edge becomes "HTTP 502" rather than a
  thrown SyntaxError (it does — pinned here);
- the 120-second badge poll keeps running in a hidden tab (CLIENT-37);
- the page uses ES2020 `?.` and `??`, a SyntaxError — and so a blank page —
  on Safari before 13.1/14, e.g. an older iPad at an on-site audit
  (CLIENT-44).

Read as source: the page is inline JS and the suite has no JS engine.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def admin():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


def _scripts(page):
    return "\n".join(re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", page, re.S))


def _api_helper(page):
    m = re.search(r"async function api\(path, opts\)\{(.*?)\n", page)
    assert m, "the admin api() helper moved"
    return m.group(1)


def test_the_admin_api_helper_sends_a_401_back_to_sign_in(admin):
    helper = _api_helper(admin)
    assert re.search(r"r\.status\s*===\s*401", helper)
    assert "location.href='/login?next=/admin'" in helper


def test_a_non_json_admin_response_is_reported_by_its_http_status(admin):
    """A 502 from the edge is an HTML page; the helper must not throw on it."""
    helper = _api_helper(admin)
    assert re.search(r"try\s*\{\s*j\s*=\s*await r\.json\(\);\s*\}\s*catch", helper)
    assert "error:'HTTP '+r.status" in helper


def test_the_badge_poll_is_still_on_an_interval(admin):
    assert "setInterval(badges, 120000)" in admin
    assert "async function badges()" in admin


@pytest.mark.xfail(strict=True, reason="CLIENT-37: the admin badge poll runs every 120s in a hidden tab")
def test_the_badge_poll_pauses_while_the_tab_is_hidden(admin):
    i = admin.index("async function badges()")
    body = admin[i:admin.index("\n}\n", i)]
    tail = admin[admin.index("setInterval(badges, 120000)") - 200:admin.index("setInterval(badges, 120000)") + 200]
    assert "document.hidden" in body + tail or "visibilityState" in body + tail


ES2020 = [
    (r"\?\.(?=[A-Za-z_$\[(])", "optional chaining ?."),
    (r"\?\?(?!=)", "nullish coalescing ??"),
]


@pytest.mark.xfail(strict=True, reason="CLIENT-44: admin.html uses ?. and ??, a SyntaxError (blank page) on Safari before 13.1/14")
@pytest.mark.parametrize("pattern,label", ES2020, ids=[lbl for _p, lbl in ES2020])
def test_the_admin_console_avoids_syntax_older_safari_cannot_parse(admin, pattern, label):
    hits = [ln.strip()[:100] for ln in _scripts(admin).split("\n") if re.search(pattern, ln)]
    assert not hits, "%s in admin.html:\n%s" % (label, "\n".join(hits[:5]))
