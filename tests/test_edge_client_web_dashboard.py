"""The web dashboard's client behaviour when things go wrong.

Every rule the dashboard already enforces by source scan (ES5, `.cbtn`,
colours) is about how the page is written. Nothing pinned how it behaves
under failure, and the CLIENT audit (Sep 2026) found that is where it
breaks:

- ~200 fetch sites parse `r.json()` without looking at the status, and a
  dozen panels sit on "Loading…" forever because their `.catch` is empty
  (CLIENT-12).
- Only Home reacts to `session_expired`; every poller keeps firing into
  401s, and none pauses while the tab is hidden (CLIENT-13, CLIENT-37).
- One failed schedule poll abandons a job that is still running, and a
  failed job shows the owner a Python traceback (CLIENT-14).
- Approve, skip and mark-posted have no busy state and no failure path, so
  a double click posts to Google twice (CLIENT-16).
- The guest blast and Ask Cavnar's Confirm re-enable after a transport
  error, inviting a blind second send (CLIENT-1, CLIENT-19).
- A reply template body is concatenated into an inline onclick (CLIENT-18);
  a session's IP is written into innerHTML unescaped (CLIENT-40).

These read templates/dashboard.html as text — the way tests/
test_frontend_rules.py does — because the behaviour lives in inline ES5 and
there is no JS engine in the suite. Each group has an anchor test that
passes today and proves the code it inspects still exists, so an xfail can
only ever be failing on the behaviour, never on a renamed function.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


@pytest.fixture(scope="module")
def src():
    with open(DASHBOARD, encoding="utf-8") as f:
        return f.read()


# ── tiny ES5 source reader ──────────────────────────────────────────────────

def _block_from(text, i):
    """The `{…}` block starting at the first `{` at or after index i,
    braces matched while skipping string literals and comments."""
    i = text.index("{", i)
    depth, j, n = 0, i, len(text)
    while j < n:
        ch = text[j]
        if ch in "'\"":
            q = ch
            j += 1
            while j < n and text[j] != q:
                j += 2 if text[j] == "\\" else 1
        elif text.startswith("//", j):
            j = text.find("\n", j)
            if j < 0:
                break
        elif text.startswith("/*", j):
            j = text.index("*/", j) + 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    raise AssertionError("unbalanced block")


def _function(text, name, containing=None):
    """Body of `function name(…){…}` or `name = function(…){…}`. When several
    functions share the name (the IIFEs reuse `load`), the one whose body
    contains `containing`."""
    pat = re.compile(r"(?:function\s+%s\s*\(|\b%s\s*=\s*function\s*\()" % (re.escape(name), re.escape(name)))
    for m in pat.finditer(text):
        body = _block_from(text, text.index(")", m.end()))
        if containing is None or containing in body:
            return body
    raise AssertionError("function %s not found" % name)


def _catch_bodies(body):
    """The handler body of every `.catch(function(…){…})` in a block."""
    out = []
    for m in re.finditer(r"\.catch\(\s*function\s*\([^)]*\)\s*", body):
        out.append(_block_from(body, m.end())[1:-1].strip())
    return out


def _has_global_fetch_guard(text, needle):
    """A central wrapper around window.fetch that handles `needle` counts
    for every call site at once — the fix CLIENT-12/13 recommend."""
    for m in re.finditer(r"window\.fetch\s*=\s*function", text):
        if needle in _block_from(text, m.end()):
            return True
    return False


# ── CLIENT-12: failures are shown, never parsed blind or swallowed ──────────

LOADERS = [
    # (function, the element it fills, what the static markup says first)
    ("lb2LoadTimeOff", "lb2-timeoff-body"),
    ("lb2LoadCovers", "lb2-covers-body"),
    ("loadAlertsSummary", "alerts-contacts-summary"),
    ("loadGuestContactsSummary", "guest-contacts-summary"),
    ("toggleTmplPicker", "tmpl-list"),
]


@pytest.mark.parametrize("fn,element", LOADERS)
def test_the_audited_loaders_still_start_on_a_loading_placeholder(src, fn, element):
    body = _function(src, fn)
    assert element in body
    assert "fetch(" in body
    assert "Loading" in src


@pytest.mark.xfail(strict=True, reason="CLIENT-12/CLIENT-58: these loaders have an empty .catch (or none) and bare-return on ok:false, so a failure leaves 'Loading…' on screen forever")
@pytest.mark.parametrize("fn,element", LOADERS)
def test_a_loader_replaces_its_loading_placeholder_when_the_request_fails(src, fn, element):
    body = _function(src, fn)
    fetches = body.count("fetch(")
    catches = [c for c in _catch_bodies(body) if c]
    assert len(catches) >= fetches, "%s: %d fetch, %d non-empty catch" % (fn, fetches, len(catches))
    # CLIENT-58: an ok:false answer that just returns leaves the placeholder
    # up and reads as "still loading" (or, once cleared, as a genuine empty).
    assert not re.search(r"if\s*\(\s*!d\.ok[^)]*\)\s*return\s*;", body), fn


def test_the_recovery_email_status_starts_on_loading(src):
    assert re.search(r'id="rec-status">Loading', src)
    assert "jget('/api/account/security-summary'" in src


@pytest.mark.xfail(strict=True, reason="CLIENT-12: the security-summary callback only handles d.ok, so a failure leaves 'Recovery email: Loading…'")
def test_the_recovery_email_status_says_so_when_the_summary_fails(src):
    i = src.index("jget('/api/account/security-summary'")
    cb = _block_from(src, i)
    assert "else" in cb or "!d.ok" in cb, cb


def test_there_are_fetch_sites_to_check(src):
    assert src.count("fetch(") > 100


@pytest.mark.xfail(strict=True, reason="CLIENT-12: ~200 fetch sites call r.json() without checking r.ok, so an HTML error page reads as a network failure")
def test_no_fetch_parses_json_without_looking_at_the_status(src):
    blind = re.findall(r"\.then\(\s*function\s*\(\s*(\w+)\s*\)\s*\{\s*return\s+\1\.json\(\)\s*;?\s*\}\s*\)", src)
    assert not blind, "%d status-blind r.json() sites" % len(blind)


# ── CLIENT-13 / CLIENT-37: pollers ──────────────────────────────────────────

def _review_stats_tick(text):
    i = text.index("var reviewPanel = document.getElementById('panel-reviews')")
    start = text.rindex("setInterval(function", 0, i)
    return _block_from(text, start)


POLLERS = {
    # name: (the interval's tick, the function that fetches)
    "activity feed, 60s": (lambda s: _function(s, "load", containing="/api/activity"),
                           lambda s: _function(s, "load", containing="/api/activity")),
    "review stats, 15s": (_review_stats_tick, lambda s: _function(s, "updateReviewStats")),
    "marketing metrics, 60s": (lambda s: _function(s, "refreshMetrics"),
                               lambda s: _function(s, "refreshMetrics")),
    "status dot, 120s": (lambda s: _function(s, "checkStatus"),
                         lambda s: _function(s, "checkStatus")),
}


def test_the_audited_pollers_are_still_on_intervals(src):
    assert "pollTimer = setInterval(load, 60000)" in src
    assert "}, 15000);" in src[src.index("var reviewPanel = document.getElementById('panel-reviews')"):][:200]
    assert "setInterval(refreshMetrics,60000)" in src
    assert "setInterval(checkStatus, 120000)" in src
    for tick, fetcher in POLLERS.values():
        assert "fetch(" in tick(src) + fetcher(src)


@pytest.mark.xfail(strict=True, reason="CLIENT-37: web pollers ignore document.hidden and fire for the life of a background tab")
@pytest.mark.parametrize("poller", sorted(POLLERS))
def test_a_poller_pauses_while_the_tab_is_hidden(src, poller):
    tick, fetcher = POLLERS[poller]
    code = tick(src) + fetcher(src)
    assert "document.hidden" in code or "visibilityState" in code or \
        _has_global_fetch_guard(src, "document.hidden")


@pytest.mark.xfail(strict=True, reason="CLIENT-13: only Home reads session_expired; pollers keep hitting 401s and the page never asks the owner to sign in")
@pytest.mark.parametrize("poller", sorted(POLLERS))
def test_a_poller_stops_and_sends_the_owner_to_sign_in_when_the_session_expires(src, poller):
    _tick, fetcher = POLLERS[poller]
    code = fetcher(src)
    assert "session_expired" in code or "401" in code or _has_global_fetch_guard(src, "session_expired")


def test_home_already_reacts_to_an_expired_session(src):
    """The one place that does it today — the pattern the rest should follow."""
    assert "if(d&&d.session_expired)location.reload()" in src


@pytest.mark.xfail(strict=True, reason="CLIENT-13: session_expired is read in exactly one place (Home); every other module fails silently")
def test_session_expiry_is_handled_outside_home(src):
    readers = src.count("session_expired")
    assert readers > 1 or _has_global_fetch_guard(src, "session_expired"), readers


# ── CLIENT-14: schedule generation polling ──────────────────────────────────

def _schedule_poll_catch(text):
    body = _function(text, "generateSchedule")
    i = body.index("_schedPollInterval = setInterval(function")
    tick = _block_from(body, i)
    catches = _catch_bodies(tick)
    assert catches, "the schedule poll has no catch"
    return catches[-1]


def test_the_schedule_poll_still_exists(src):
    assert "/api/schedule-status/" in _function(src, "generateSchedule")
    assert _schedule_poll_catch(src)
    assert "data.error" in _function(src, "_schedHandleResult")


@pytest.mark.xfail(strict=True, reason="CLIENT-14: the first failed poll clears the interval and alerts 'Network error' while the job keeps running on the server")
def test_one_failed_schedule_poll_does_not_abandon_the_job(src):
    catch = _schedule_poll_catch(src)
    stop = catch.find("clearInterval(_schedPollInterval)")
    assert stop < 0 or re.search(r"\bif\s*\(", catch[:stop]), catch


@pytest.mark.xfail(strict=True, reason="CLIENT-14: a failed schedule job's traceback is shown to the owner in an alert()")
def test_a_failed_schedule_job_never_shows_a_traceback(src):
    assert "traceback" not in _function(src, "_schedHandleResult")


# ── CLIENT-15: long names and narrow screens ────────────────────────────────

def _css_rules_for(text, cls):
    """Declaration blocks of every CSS rule whose selector names `cls` as
    the element itself (not a descendant of it)."""
    own = re.compile(r"%s(?![\w-])(?:\s*$|:)" % re.escape(cls))
    out = []
    for css in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S):
        for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
            if cls not in m.group(1):
                continue
            if any(own.search(s.strip()) for s in m.group(1).split(",")):
                out.append(m.group(2).replace(" ", ""))
    return out


def test_the_header_name_rule_exists(src):
    assert _css_rules_for(src, ".hdr-restaurant")
    assert any("height:56px" in d for d in _css_rules_for(src, ".hdr"))


@pytest.mark.xfail(strict=True, reason="CLIENT-15: .hdr-restaurant has no truncation, so a long restaurant name wraps to 5 lines inside the 56px header")
def test_a_long_restaurant_name_is_truncated_in_the_header(src):
    decls = "".join(_css_rules_for(src, ".hdr-restaurant"))
    assert "text-overflow:ellipsis" in decls and "white-space:nowrap" in decls and "overflow:hidden" in decls, decls


@pytest.mark.xfail(strict=True, reason="CLIENT-15: nothing collapses .hdr-right on a phone, so Sign out and Account sit off-screen at 320-375px")
def test_the_header_right_side_collapses_on_a_phone(src):
    found = False
    for m in re.finditer(r"@media[^{]*max-width:\s*(\d+)px[^{]*\{", src):
        if int(m.group(1)) <= 480 and ".hdr-right" in _block_from(src, m.end() - 1):
            found = True
    assert found


# ── CLIENT-16: review actions ───────────────────────────────────────────────

REVIEW_ACTIONS = ["approveR", "skipR", "markPosted", "saveDraft"]


@pytest.mark.parametrize("fn", REVIEW_ACTIONS)
def test_the_review_action_handlers_still_post(src, fn):
    assert "fetch(" in _function(src, fn)


def test_the_busy_helper_exists(src):
    assert "function cbtnBusy(btn, label)" in src


@pytest.mark.xfail(strict=True, reason="CLIENT-16: review actions never disable their button, so a double click posts to Google and fires the webhook twice")
@pytest.mark.parametrize("fn", REVIEW_ACTIONS)
def test_a_review_action_blocks_a_second_click_while_in_flight(src, fn):
    body = _function(src, fn)
    assert "cbtnBusy(" in body or "busy(" in body or ".disabled=true" in body.replace(" ", ""), fn


def _reports_failure(body):
    catches = [c for c in _catch_bodies(body) if "toast" in c]
    handles_not_ok = bool(re.search(r"if\s*\(\s*!\s*d\.ok|\}\s*else\s*\{?\s*toast", body))
    return bool(catches) and handles_not_ok


def test_skip_already_tells_the_owner_when_it_failed(src):
    """skipR has both halves (else-toast and a catch) — the shape the other
    two should copy."""
    assert _reports_failure(_function(src, "skipR"))


@pytest.mark.xfail(strict=True, reason="CLIENT-16: approveR and markPosted have no else for ok:false and no .catch, so a failure is silent")
@pytest.mark.parametrize("fn", ["approveR", "markPosted"])
def test_a_review_action_tells_the_owner_when_it_failed(src, fn):
    assert _reports_failure(_function(src, fn)), fn


# ── CLIENT-1 / CLIENT-19: sends with side effects ───────────────────────────

def test_the_guest_blast_and_ask_confirm_still_exist(src):
    assert "/api/guest-campaign/send" in _function(src, "sendGuestCampaign")
    assert _catch_bodies(_function(src, "sendGuestCampaign"))
    assert _catch_bodies(_function(src, "_runAskCavnarProposal"))


def test_the_guest_blast_asks_before_it_sends(src):
    body = _function(src, "sendGuestCampaign")
    assert body.index("confirm(") < body.index("fetch(")


@pytest.mark.xfail(strict=True, reason="CLIENT-1: after a transport error the blast button is re-enabled with 'try again', though the texts may already have gone out")
def test_a_guest_blast_that_lost_its_response_does_not_invite_a_blind_resend(src):
    catch = _catch_bodies(_function(src, "sendGuestCampaign"))[-1]
    assert "disabled = false" not in catch and "try again" not in catch.lower(), catch


@pytest.mark.xfail(strict=True, reason="CLIENT-19: Ask Cavnar's Confirm re-enables silently after a network error, inviting a duplicate side-effecting action")
def test_ask_confirm_after_a_network_error_says_the_outcome_is_unknown(src):
    catch = _catch_bodies(_function(src, "_runAskCavnarProposal"))[-1]
    assert "disabled = false" not in catch, catch
    assert "textContent" in catch or "toast(" in catch, catch


# ── CLIENT-18: reply templates ──────────────────────────────────────────────

def test_the_template_picker_still_renders_rows(src):
    body = _function(src, "_renderTmplPicker")
    assert "insertTemplate" in body and "t.body" in body


@pytest.mark.xfail(strict=True, reason="CLIENT-18: a template body is concatenated into an inline onclick; a double quote ends the attribute (breaks the row, can inject attributes)")
def test_no_template_text_is_concatenated_into_an_inline_onclick(src):
    body = _function(src, "_renderTmplPicker")
    for m in re.finditer(r"onclick=\"[^\"]*'\s*\+\s*([^+]+?)\s*\+", body):
        assert "body" not in m.group(1) and "title" not in m.group(1), m.group(0)


# ── CLIENT-40: signed-in sessions list ──────────────────────────────────────

def test_the_sessions_list_still_renders_the_ip(src):
    assert "s.ip_address" in _function(src, "loadSessions")


@pytest.mark.xfail(strict=True, reason="CLIENT-40: loadSessions writes s.ip_address (client-supplied X-Forwarded-For) into innerHTML unescaped")
def test_the_sessions_list_escapes_every_field(src):
    body = _function(src, "loadSessions")
    for field in ("s.ip_address", "s.last_active", "deviceLabel"):
        for m in re.finditer(r"\+\s*%s\s*\+" % re.escape(field), body):
            raise AssertionError("%s concatenated into HTML unescaped" % field)


# ── CLIENT-43: the status dot ───────────────────────────────────────────────

def test_the_status_dot_still_maps_the_overall_state(src):
    body = _function(src, "updateStatusDots")
    assert "DOT_COLORS[overall]" in body


@pytest.mark.xfail(strict=True, reason="CLIENT-43: an unknown or unreadable /api/status paints the dot green ('operational')")
def test_an_unknown_status_is_not_painted_operational(src):
    body = _function(src, "updateStatusDots")
    assert "'#22c55e'" not in body.split("DOT_COLORS[overall]", 1)[1][:40], body


# ── CLIENT-38: per-load and per-tab writes ──────────────────────────────────

def test_the_theme_is_forced_dark_before_paint(src):
    assert "document.documentElement.setAttribute('data-theme','dark')" in src[:2000]


@pytest.mark.xfail(strict=True, reason="CLIENT-38: every dashboard load POSTs /api/theme (a database write) although the theme is hard-forced dark")
def test_the_dashboard_does_not_post_the_theme_on_load(src):
    assert "fetch('/api/theme'" not in src


@pytest.mark.xfail(strict=True, reason="CLIENT-38: the per-tab /api/log-activity POST has no .catch (an unhandled rejection on every failed tab switch)")
def test_the_tab_switch_activity_log_handles_its_own_failure(src):
    i = src.index("fetch('/api/log-activity'")
    stmt = src[i:src.index(";\n", i)]
    assert ".catch(" in stmt, stmt


# ── CLIENT-48: errors through alert() ───────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="CLIENT-48: schedule generation, profile save and staff actions report errors with alert()")
def test_the_dashboard_reports_errors_without_alert(src):
    scripts = "\n".join(re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", src, re.S))
    calls = re.findall(r"(?<![\w.])alert\(", scripts)
    assert not calls, "%d alert() calls" % len(calls)
