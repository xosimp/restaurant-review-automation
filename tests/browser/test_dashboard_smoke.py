"""Browser smoke of the web dashboard (audit 10/26/26 A06-05 / A01-14,
roadmap P2).

Every other dashboard test reads the template's source or runs an extracted
function under node with a stubbed DOM; a runtime TypeError in one module's
init (a renamed element id) stops the rest of its script block, the tab is
blank for every owner, and all of those still pass. This drives a real
Chromium against a real, keyless app (tests/browser/keyless_server.py: a
throwaway volume, no provider keys, no .env, no network, no scheduler).

At a phone width (390px) and a desktop width (1280px) the seeded demo owner
signs in through the login form, as a person does, and opens each top-level
place in turn — Home, Reviews, Labor, Food Cost, Marketing, Intel, Account,
Ask, then Bar (its own address). For each: no uncaught page error, no
console error, the place's root element visible, no same-origin request
answered 4xx/5xx, and no "Cavnar AI was updated" bar. Each width signs in
its own session, as two devices would. Requests to any other origin (web
fonts, analytics) are answered empty by the browser itself, so nothing
leaves the machine and nothing external can fail the run. The request
budgets for people (request_limits) are off in this app: a script opens
nine places in seconds.

One answer is not a failure: a 503 whose body says the model "isn't
configured on this server" — the truth here, since the app has no key.

Then the stale-tab bar (templates/_csrf_fetch.html) in the real page: none
while answers name the page's build, one when an answer names another, and
Reload reloads. Last, the app made no attempt to reach past loopback.

Marked `browser`: skipped where playwright is not installed; CI runs it in
its own job (`python -m playwright install --with-deps chromium` first).
"""
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SERVER = os.path.join(HERE, "keyless_server.py")
BUILD = "5a0ce5a0ce5a"                  # the build the throwaway app says it is (GIT_COMMIT)
WIDTHS = {"phone": (390, 844), "desktop": (1280, 900)}
_ENV_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")

# (place, how it opens, its root element), in tour order: "click" presses a
# tab or button; Ask is closed again before the next place; Bar is a page of
# its own address that covers the app, so it is last.
PLACES = [
    ("home", ("click", "#tab-home"), "#panel-home"),
    ("reviews", ("click", "#tab-reviews"), "#panel-reviews"),
    ("labor", ("click", "#tab-labor"), "#panel-labor"),
    ("food-cost", ("click", "#tab-inventory"), "#panel-inventory"),
    ("marketing", ("click", "#tab-marketing"), "#panel-marketing"),
    ("intel", ("click", "#tab-competitor"), "#panel-competitor"),
    ("account", ("click", "#tab-account"), "#panel-account"),
    ("ask", ("click", "#ask-cavnar-fab"), "#ask-cavnar-panel"),
    ("bar", ("goto", "/bar"), "#bar"),
]
NOT_CONFIGURED = "isn't configured on this server"     # ai_utils.AINotConfigured, said to the owner


def _tail(path, n=4000):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()[-n:]
    except OSError:
        return ""


@pytest.fixture(scope="module")
def app_server():
    """The keyless app on 127.0.0.1:<free port>, killed (with its process
    group) and its volume deleted when the module ends."""
    vol = tempfile.mkdtemp(prefix="cavnar-smoke-")
    info_path = os.path.join(vol, "server.json")
    log_path = os.path.join(vol, "server.log")
    password = "Smoke-" + secrets.token_urlsafe(18)
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if os.environ.get(k)}
    env.update(
        SECRET_KEY=secrets.token_hex(16), CAVNAR_PIN_PEPPER="smoke-pepper", DEMO_PASSWORD=password,
        HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0", RUN_SCHEDULER_IN_WEB="0", FLASK_SKIP_DOTENV="1",
        GIT_COMMIT=BUILD, HOME_SNAPSHOT_COLD="request", AI_SHADOW_REVIEW="0", LEARNING_HOLDOUT_PCT="0",
        # The per-login and per-restaurant request budgets (request_limits)
        # are sized for people; a scripted tour opens nine places in seconds
        # on two devices, and would be refused by the clock, not by a bug.
        # Their own tests hold them; the wrapper's in-flight retry is
        # tests/test_stale_tab_reload.py's.
        REQUEST_LIMITS="0",
        CAVNAR_SEASONS_DIR=os.path.join(ROOT, "tests", "fixtures", "seasons"), PYTHONDONTWRITEBYTECODE="1",
    )
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, SERVER, vol, info_path], cwd=ROOT, env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        deadline = time.monotonic() + 150
        while not os.path.exists(info_path):
            if proc.poll() is not None:
                pytest.fail("the keyless app exited (%s) before listening:\n%s" % (proc.returncode, _tail(log_path)))
            if time.monotonic() > deadline:
                pytest.fail("the keyless app did not listen within 150s:\n" + _tail(log_path))
            time.sleep(0.2)
        with open(info_path, encoding="utf-8") as fh:
            info = json.load(fh)
        info.update(base="http://127.0.0.1:%d" % info["port"], password=password, vol=vol, log=log_path)
        yield info
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=15)
        log.close()
        shutil.rmtree(vol, ignore_errors=True)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except sync_api.Error as e:   # the browser binary is a separate download
            msg = "chromium is not installed (python -m playwright install chromium): %s" % str(e)[:300]
            if os.getenv("CI"):
                pytest.fail(msg)
            pytest.skip(msg)
        try:
            yield b
        finally:
            b.close()


def _new_page(browser, server, width):
    """A fresh browser profile at `width`, every other origin answered
    empty in the browser."""
    w, h = WIDTHS[width]
    ctx = browser.new_context(viewport={"width": w, "height": h}, service_workers="block")
    base = server["base"]

    def offline(route):
        url = route.request.url
        kind = "text/css" if (".css" in url or "fonts.googleapis" in url) else (
            "application/javascript" if ".js" in url else "text/plain")
        route.fulfill(status=200, body="", content_type=kind)
    ctx.route(lambda url: not url.startswith(base), offline)
    page = ctx.new_page()
    return ctx, page, Watch(page, base)


class Watch:
    """What went wrong on one page, read in windows (one per place).

    One answer is not a failure: a 503 whose body says the model "isn't
    configured on this server" — the AI panels' honest answer in an app that
    has no key by design. Chrome's own "Failed to load resource" line for a
    same-origin answer is judged by that answer, not twice. A failed answer's
    body is read when it arrives (Chromium may drop it later)."""

    _RESOURCE = "Failed to load resource: the server responded with a status of"

    def __init__(self, page, base):
        self.base = base
        self.page_errors, self.console_errors, self.failed = [], [], []
        self.inflight = set()
        page.on("pageerror", lambda e: self.page_errors.append(str(e)[:400]))
        page.on("console", self._console)
        page.on("request", lambda r: self.inflight.add(r) if r.url.startswith(base) else None)
        page.on("requestfailed", lambda r: self.inflight.discard(r))
        page.on("response", self._response)

    def _response(self, r):
        # Answered is settled: Chromium never reports a fetch whose body
        # nobody reads (the tab ping, /api/log-activity) as finished.
        self.inflight.discard(r.request)
        if r.status < 400 or not r.url.startswith(self.base):
            return
        try:
            body = r.text()
        except sync_api.Error as e:
            body = "<body unavailable: %s>" % str(e)[:100]
        if r.status == 503 and NOT_CONFIGURED in body:
            return
        self.failed.append("HTTP %s %s %s %s" % (r.status, r.request.method, r.url[len(self.base):],
                                                 body[:200].replace("\n", " ")))

    def _console(self, m):
        if m.type != "error":
            return
        where = (m.location or {}).get("url") or ""
        if m.text.startswith(self._RESOURCE) and where.startswith(self.base):
            return
        self.console_errors.append(m.text[:400])

    def mark(self):
        return (len(self.page_errors), len(self.console_errors), len(self.failed))

    def problems(self, since=(0, 0, 0)):
        pe, ce, fr = since
        return (["page error: " + e for e in self.page_errors[pe:]]
                + ["console error: " + e for e in self.console_errors[ce:]] + self.failed[fr:])

    def settle(self, page, quiet_ms=600, cap_ms=10000):
        """Until no same-origin request has been open for `quiet_ms` (the
        dashboard polls, so the network is never idle for long), at most
        `cap_ms`."""
        waited = quiet = 0
        while waited < cap_ms and quiet < quiet_ms:
            page.wait_for_timeout(100)
            waited += 100
            quiet = 0 if self.inflight else quiet + 100


def _sign_in(page, server):
    page.goto(server["base"] + "/login", wait_until="load")
    page.fill('input[name="username"]', server["username"])
    page.fill('input[name="password"]', server["password"])
    with page.expect_navigation(wait_until="load"):
        # Enter submits the form, as a person does; the sign-in button's
        # light never stops moving, so a pointer click waits on it forever.
        page.press('input[name="password"]', "Enter")


def _tour(browser, server, width):
    """{"login": problems, place: problems, …} for one width."""
    ctx, page, watch = _new_page(browser, server, width)
    out = {}
    try:
        mark = watch.mark()
        _sign_in(page, server)
        login = []
        if page.url.rstrip("/") != server["base"]:
            login.append("signing in landed on %s" % page.url)
        try:
            page.locator("#panel-home").wait_for(state="visible", timeout=10000)
        except sync_api.Error:
            login.append("Home is not on screen after signing in")
        watch.settle(page)
        out["login"] = login + watch.problems(mark)
        for place, (kind, target), root in PLACES:
            mark = watch.mark()
            found = []
            try:
                if kind == "goto":
                    page.goto(server["base"] + target, wait_until="load")
                else:
                    page.locator(target).click()
                page.locator(root).first.wait_for(state="visible", timeout=10000)
                watch.settle(page)
                if not page.locator(root).first.is_visible():
                    found.append("%s is not visible" % root)
                if page.locator("#cav-updated").count():
                    found.append("the 'Cavnar AI was updated' bar showed with no new build")
                if place == "ask":
                    page.locator(target).click()          # closed again, so the next tab is reachable
            except sync_api.Error as e:
                found.append("%s: %s" % (type(e).__name__, str(e).splitlines()[0][:300]))
                page.goto(server["base"] + "/", wait_until="load")     # the next place starts clean
            out[place] = found + watch.problems(mark)
    finally:
        ctx.close()
    return out


@pytest.fixture(scope="module")
def tours(app_server, browser):
    return {width: _tour(browser, app_server, width) for width in WIDTHS}


def _report(tours, key):
    return ["%s @%s: %s" % (key, width, p) for width in WIDTHS for p in tours[width][key]]


def test_the_demo_owner_signs_in_and_lands_on_home(tours):
    failures = _report(tours, "login")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("place", [p[0] for p in PLACES])
def test_each_place_opens_clean_at_phone_and_desktop_width(tours, place):
    failures = _report(tours, place)
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("width", list(WIDTHS))
def test_a_new_build_offers_one_reload(app_server, browser, width):
    """The page carries its build; while answers name it nothing shows,
    and once an answer names another build one bar offers a reload — on
    top of the page, its Reload reachable, at both widths."""
    ctx, page, watch = _new_page(browser, app_server, width)
    try:
        _sign_in(page, app_server)
        watch.settle(page)
        assert page.evaluate("window.cavBuildChanged()") is False
        assert page.locator("#cav-updated").count() == 0
        api = app_server["base"] + "/api/"

        def newer(route):
            resp = route.fetch()
            headers = dict(resp.headers)
            headers["x-cavnar-build"] = "0123456789ab"
            route.fulfill(response=resp, headers=headers)

        page.route(lambda url: url.startswith(api), newer)
        page.evaluate("fetch('/api/activity')")
        bar = page.locator("#cav-updated")
        bar.wait_for(state="visible", timeout=10000)
        assert "Cavnar AI was updated." in bar.inner_text()
        page.evaluate("fetch('/api/activity')")
        watch.settle(page)
        assert page.locator("#cav-updated").count() == 1, "one bar, however many answers"
        page.unroute_all()
        reload = bar.get_by_role("button", name="Reload")
        box = reload.bounding_box()
        on_top = page.evaluate(
            "(p) => { var e = document.elementFromPoint(p.x, p.y); return e && e.closest('#cav-updated') "
            "? '' : (e ? e.tagName + '#' + e.id + '.' + e.className : 'nothing'); }",
            {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2})
        assert on_top == "", "the Reload button is covered by " + on_top
        with page.expect_navigation(wait_until="load"):
            reload.click()
        watch.settle(page)
        assert page.locator("#cav-updated").count() == 0
        assert page.evaluate("window.cavBuildChanged()") is False
    finally:
        ctx.close()
    assert not watch.page_errors, watch.page_errors


def test_the_keyless_app_reached_for_no_network(app_server, tours):
    """After every place: nothing a page asked for made the app reach past
    loopback (with no keys it could only have failed, and a provider call
    on a page load is a cost in production)."""
    attempts = _tail(os.path.join(app_server["vol"], "network_attempts.log"))
    assert attempts == "", "the app tried to reach: " + attempts
