"""The web client, seen through the real app: what `/api/*` sends back when
a request fails, and what the dashboard shell renders for names real
restaurants have.

Why these matter (CLIENT audit, Sep 2026):
- The dashboard's ~200 fetch sites call `r.json()` without looking at the
  status. `/api/*` answering a 404, 405, 413 or 500 with an HTML page means
  every server error reads as "check your connection" and panels sit on
  "Loading…" forever (CLIENT-12). `/mobile/api/*` already answers in JSON;
  the web half never got the same handlers.
- A location named "Simple EJ's" (here "Maple & Rye's") is escaped to `&#39;` inside an inline
  `onclick`, which the browser decodes back to `'` before compiling the
  handler — a SyntaxError, and the switcher is a dead click (CLIENT-17).
- The Home kicker is server-rendered as "September 22, 2026" on first
  paint, against the M/D/YY rule.

hosted_dashboard boots the whole app at import (schema, seed thread,
blueprints), so — as tests/test_home_page_renders.py does — it runs in a
subprocess against a scratch volume with an empty reviews.db pre-created,
every outbound key blanked and `requests` blocked outright. The script
runs once per module and hands every observation back as JSON.
"""
import html as _html
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = r'''
import io, json, os, sqlite3, sys
vol, out_path = sys.argv[1], sys.argv[2]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()   # pre-create: no legacy adoption
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", STRIPE_SECRET_KEY="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0",
                  GOOGLE_PLACES_API_KEY="", PERPLEXITY_API_KEY="")
os.environ.pop("RAILWAY_ENVIRONMENT", None); os.environ.pop("RAILWAY_PROJECT_ID", None)
import requests
def _blocked(*a, **k):
    raise RuntimeError("network blocked in test subprocess")
requests.get = requests.post = requests.put = requests.delete = requests.request = _blocked
requests.Session.request = lambda self, *a, **k: _blocked()

import hosted_dashboard as h
import auth, models
from models import Restaurant

def _boom():
    raise RuntimeError("edge-case probe: a handler raised")
h.app.add_url_rule("/api/__edge_boom", "edge_web_boom", _boom)
h.app.add_url_rule("/mobile/api/__edge_boom", "edge_mobile_boom", _boom)

# Names deliberately not the seeded demo accounts' (demo_seed rewrites those).
rid_a = models.create_restaurant(Restaurant(name="Maple & Rye's", owner_email="erik@x.test", module_reviews=1))
rid_b = models.create_restaurant(Restaurant(name="Maple & Rye's", owner_email="erik@x.test", module_reviews=1))
conn = models.get_conn()
conn.execute("UPDATE restaurants SET location_group='EJ Group', location_name=? WHERE id=?", ("Maple & Rye's — Downtown", rid_a))
conn.execute("UPDATE restaurants SET location_group='EJ Group', location_name=? WHERE id=?", ("Nonna Tia's", rid_b))
conn.execute("UPDATE restaurants SET billing_status='active' WHERE id IN (?,?)", (rid_a, rid_b))
conn.commit(); conn.close()
uid = auth.create_user(rid_a, "erik", "erik@x.test", "correct-horse-battery", is_admin=False)
auth.upsert_membership(uid, rid_a, "owner")   # owners hold location.switch
tok = auth.create_session(uid, ip_address="127.0.0.1", user_agent="edge-test", restaurant_id=rid_a)

c = h.app.test_client()
c.set_cookie("session_token", tok)
c.set_cookie("csrf_js", "edge-csrf")
res = {}

def shape(r):
    return {"status": r.status_code, "content_type": r.headers.get("Content-Type", ""),
            "body": r.get_data(as_text=True)[:400]}

page = c.get("/")
res["dashboard"] = {"status": page.status_code, "html": page.get_data(as_text=True)}
res["web_404"] = shape(c.get("/api/does-not-exist"))
res["web_405"] = shape(c.post("/api/schedule-status/some-job", headers={"X-CSRF": "edge-csrf"}))
res["web_413"] = shape(c.post("/api/labor/covers", data=b"x" * (6 * 1024 * 1024),
                              headers={"Content-Type": "application/json", "X-CSRF": "edge-csrf"}))
res["web_500"] = shape(c.get("/api/__edge_boom"))
res["mobile_404"] = shape(c.get("/mobile/api/does-not-exist"))
res["mobile_500"] = shape(c.get("/mobile/api/__edge_boom"))
json.dump(res, open(out_path, "w"))
print("EDGE-HARNESS-OK")
'''


@pytest.fixture(scope="module")
def app_run():
    vol = tempfile.mkdtemp(prefix="cavnar-edge-web-")
    out_path = os.path.join(vol, "result.json")
    proc = subprocess.run([sys.executable, "-c", SCRIPT, vol, out_path], cwd=ROOT,
                          capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0 and "EDGE-HARNESS-OK" in proc.stdout, \
        proc.stdout[-1500:] + "\n" + proc.stderr[-2500:]
    with open(out_path) as f:
        return json.load(f)


def _is_json_error(resp):
    if "application/json" not in resp["content_type"]:
        return False
    try:
        body = json.loads(resp["body"])
    except ValueError:
        return False
    return body.get("ok") is False and bool(body.get("error"))


# ── /api/* errors are JSON, like /mobile/api/* already are (CLIENT-12) ──────

def test_the_harness_rendered_the_dashboard(app_run):
    """Guards the xfails below: they must fail on the behaviour, never on a
    harness that didn't boot or didn't sign in."""
    assert app_run["dashboard"]["status"] == 200
    assert 'id="hb-kicker"' in app_run["dashboard"]["html"]


def test_an_unknown_mobile_api_path_answers_in_json(app_run):
    assert app_run["mobile_404"]["status"] == 404
    assert _is_json_error(app_run["mobile_404"])


def test_a_mobile_api_handler_that_raises_answers_in_json(app_run):
    assert app_run["mobile_500"]["status"] == 500
    assert _is_json_error(app_run["mobile_500"])


@pytest.mark.xfail(strict=True, reason="CLIENT-12: /api/* 404 is an HTML page, so the dashboard's r.json() throws and reads it as a network failure")
def test_an_unknown_web_api_path_answers_in_json(app_run):
    assert app_run["web_404"]["status"] == 404
    assert _is_json_error(app_run["web_404"]), app_run["web_404"]


@pytest.mark.xfail(strict=True, reason="CLIENT-12: /api/* 405 is Flask's default HTML page")
def test_a_wrong_method_on_a_web_api_path_answers_in_json(app_run):
    assert app_run["web_405"]["status"] == 405
    assert _is_json_error(app_run["web_405"]), app_run["web_405"]


@pytest.mark.xfail(strict=True, reason="CLIENT-12: an /api/* body over MAX_CONTENT_LENGTH gets Flask's HTML 413 page (no 413 handler)")
def test_an_oversized_web_api_body_answers_in_json(app_run):
    assert app_run["web_413"]["status"] == 413
    assert _is_json_error(app_run["web_413"]), app_run["web_413"]


@pytest.mark.xfail(strict=True, reason="CLIENT-12: an /api/* handler that raises returns the HTML 500 page")
def test_a_web_api_handler_that_raises_answers_in_json(app_run):
    assert app_run["web_500"]["status"] == 500
    assert _is_json_error(app_run["web_500"]), app_run["web_500"]


# ── the dashboard shell with real-world names ───────────────────────────────

def _switcher_handlers(page):
    """Every location-switcher button's onclick, as the browser sees it:
    the attribute value after HTML entity decoding, which is what gets
    compiled as JavaScript."""
    raw = re.findall(r'<button onclick="(switchLocation\([^"]*)"', page)
    return [_html.unescape(v) for v in raw]


def test_a_two_location_owner_gets_the_location_switcher(app_run):
    handlers = _switcher_handlers(app_run["dashboard"]["html"])
    assert len(handlers) == 2, handlers


@pytest.mark.xfail(strict=True, reason="CLIENT-17: an apostrophe in a location name is decoded back into the inline onclick's JS string, a SyntaxError")
def test_the_location_switcher_survives_an_apostrophe_in_the_name(app_run):
    handlers = _switcher_handlers(app_run["dashboard"]["html"])
    assert handlers
    for js in handlers:
        # After decoding, a name inside a single-quoted JS literal must not
        # carry a bare apostrophe: count the unescaped single quotes.
        bare_quotes = len(re.findall(r"(?<!\\)'", js))
        assert bare_quotes % 2 == 0 and "Rye's" not in js and "Tia's" not in js, js


@pytest.mark.xfail(strict=True, reason="Dates rule (CLIENT audit web edge 24): the Home kicker is server-rendered as 'September 22, 2026' on first paint, not M/D/YY")
def test_the_home_kicker_first_paint_is_an_mdy_date(app_run):
    m = re.search(r'id="hb-kicker">([^<]*)<', app_run["dashboard"]["html"])
    assert m
    assert re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", m.group(1).strip()), m.group(1)


def test_the_rendered_dashboard_does_not_disable_pinch_zoom(app_run):
    """Accessibility: zoom is the fallback when text is too small."""
    m = re.search(r'<meta name="viewport" content="([^"]*)"', app_run["dashboard"]["html"])
    assert m
    content = m.group(1).replace(" ", "").lower()
    assert "user-scalable=no" not in content and "user-scalable=0" not in content
    cap = re.search(r"maximum-scale=([\d.]+)", content)
    assert cap is None or float(cap.group(1)) >= 2, content
