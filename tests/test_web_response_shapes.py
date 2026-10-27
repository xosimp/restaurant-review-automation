"""The web dashboard's main JSON reads, held to their shape (blind re-audit
10/27/26, B4 "[P2] the web dashboard has no payload-shape contract").

The dashboard's inline ES5 reads keys straight off each answer (`p.labor_pct`,
`d.profitability.prime_cost_pct`): a key renamed or re-typed on the server
renders "undefined" or a blank tile, with no console error, and every test
passed. tests/test_client_paths_resolve.py holds the paths only. This is the
phone's pattern (tests/test_ios_response_shapes.py) for the browser:

* each read is captured from the real route — as the owner, and the
  manager's own views where the figures are per viewer — on the fictional
  demo store the phone captures use (ios_shape_capture.build_world), signed
  in with a web session cookie through every blueprint the app registers
  (blueprints.url_map_app), and compared with its committed fixture
  tests/fixtures/web/<name>.json: every key, the kind of every value;
* <name>.v1.json is what the JavaScript on screen reads. A tab left open
  across a deploy runs the old script against the new server (the stale-tab
  bar offers a reload, it does not force one), so a key in v1 that the
  server stops sending fails (`removed_paths`) — fields are added, never
  renamed or removed without changing the reader in the same commit and
  replacing the v1 copy deliberately (CAVNAR_WRITE_WEB_V1=1).

Re-capture after an intended payload change:
    CAVNAR_WRITE_WEB_FIXTURES=1 python -m pytest -q tests/test_web_response_shapes.py
"""
import json
import os
import re

import pytest

import auth
import blueprints
import ios_shape_capture as cap
import test_ios_response_shapes as ios
from test_ios_schedule_payload_contract import shape_mismatches

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "web")

# name -> (owner|manager|manager_food, path, query): what each dashboard
# panel reads on open.
ROUTES = {
    "home_brief": ("owner", "/api/home/brief", {}),
    "actions": ("owner", "/api/actions", {}),
    "value": ("owner", "/api/value", {}),
    "goals": ("owner", "/api/goals", {}),
    "dna": ("owner", "/api/dna", {}),
    "data_health": ("owner", "/api/data-health", {}),
    "review_stats": ("owner", "/api/review-stats", {}),
    "reviews_why_line": ("owner", "/api/reviews/why-line", {}),
    "sentiment_trend": ("owner", "/api/sentiment-trend", {}),
    "labor_team": ("owner", "/api/labor/team", {}),
    "labor_roster": ("owner", "/api/labor/roster", {}),
    "labor_time_off": ("owner", "/api/labor/time-off", {}),
    "labor_trend": ("owner", "/api/labor-trend", {}),
    "labor_gap": ("owner", "/api/labor-gap", {}),
    "labor_inbox": ("owner", "/api/labor/inbox", {}),
    "food_cost_cfo": ("owner", "/api/food-cost/cfo", {}),
    "food_cost_bar": ("owner", "/api/food-cost/bar", {}),
    "food_cost_cogs": ("owner", "/api/food-cost/cogs", {}),
    "food_cost_par": ("owner", "/api/food-cost/par-suggestions", {}),
    "food_cost_dishes": ("owner", "/api/food-cost/dish-scorecard", {}),
    "intel_movement": ("owner", "/api/intel/movement", {}),
    "competitor_intel": ("owner", "/api/competitor-intel", {}),
    "marketing_header": ("owner", "/api/marketing/header", {}),
    "marketing_performance": ("owner", "/api/mkt-performance", {}),
    "guest_overview": ("owner", "/api/guest-overview", {}),
    "people": ("owner", "/api/people", {}),
    "task_sheets": ("owner", "/api/task-sheets", {}),
    "staff_brief": ("owner", "/api/staff-brief", {}),
    "team_inbox": ("owner", "/api/team/inbox", {}),
    "account_settings": ("owner", "/api/account-settings", {}),
    "alert_settings": ("owner", "/api/alert-settings", {}),
    "billing_info": ("owner", "/api/billing-info", {}),
    "dsr_list": ("owner", "/api/dsr", {"limit": "1"}),
    # The manager's own figures (labor hourly, not all-in; WS3).
    "home_brief_manager": ("manager", "/api/home/brief", {}),
    "labor_team_manager": ("manager", "/api/labor/team", {}),
    "food_cost_cfo_manager": ("manager_food", "/api/food-cost/cfo", {}),
}
# The night's report and its week, as the owner and as a manager.
DSR_ROUTES = ("dsr_night", "dsr_week")
NAMES = tuple(ROUTES) + DSR_ROUTES


def _path(name):
    return os.path.join(FIXTURES, f"{name}.json")


def _load(name):
    with open(_path(name), encoding="utf-8") as fh:
        return json.load(fh)


def _write(name, payload):
    os.makedirs(FIXTURES, exist_ok=True)
    text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
    text = ios._VOLATILE.sub(r'\1"<volatile>"', text)
    text = ios._FEED_TOKEN.sub(r"\1<volatile>\2", text)
    text = _JOIN_LINK.sub(r"\1<volatile>", text)
    with open(_path(name), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")


# A guest join link's code (guest_overview): the capture database's own.
_JOIN_LINK = re.compile(r"(/join/)[A-Za-z0-9_.-]{6,}")


def capture(monkeypatch, db_path):
    """{name: payload}; the DSR pair as {"owner", "manager"}."""
    rid, tokens = cap.build_world(monkeypatch, db_path)
    conn = cap.models.get_conn()
    try:
        ids = {who: conn.execute("SELECT id FROM users WHERE username=?", (login[0],)).fetchone()["id"]
               for who, login in (("owner", cap.OWNER), ("manager", cap.MANAGER), ("manager_food", cap.MANAGER_FOOD))}
    finally:
        conn.close()
    cookies = {who: auth.create_session(uid, restaurant_id=rid) for who, uid in ids.items()}
    app = blueprints.url_map_app(template_folder=os.path.join(ROOT, "templates"))
    out, problems = {}, []

    def get(who, path, query):
        c = app.test_client()
        c.set_cookie("session_token", cookies[who])
        r = c.get(path, query_string=query)
        if r.status_code != 200 or not r.is_json:
            problems.append(f"{path} as {who}: {r.status_code} {r.get_data(as_text=True)[:300]}")
            return None
        return r.get_json()
    for name, (who, path, query) in ROUTES.items():
        out[name] = get(who, path, query)
    day = (((out.get("dsr_list") or {}).get("reports") or [{}])[0] or {}).get("business_date")
    if not day:
        problems.append(f"no DSR night to read: {out.get('dsr_list')}")
    else:
        out["dsr_night"] = {v: get(v, f"/api/dsr/{day}", {}) for v in ("owner", "manager")}
        out["dsr_week"] = {v: get(v, "/api/dsr/week", {"date": day}) for v in ("owner", "manager")}
    if problems:
        raise AssertionError("dashboard reads did not answer:\n" + "\n".join(problems))
    return out


_LIVE = {}


@pytest.fixture
def live(db_path, monkeypatch):
    if "payloads" not in _LIVE:
        payloads = capture(monkeypatch, db_path)
        if os.environ.get("CAVNAR_WRITE_WEB_FIXTURES") == "1":
            for name in NAMES:
                _write(name, payloads[name])
                if not os.path.exists(_path(name + ".v1")) or os.environ.get("CAVNAR_WRITE_WEB_V1") == "1":
                    _write(name + ".v1", payloads[name])
        _LIVE["payloads"] = payloads
    return _LIVE["payloads"]


def test_the_dashboard_payloads_match_their_fixtures(live):
    problems = []
    for name in NAMES:
        assert os.path.exists(_path(name)), f"{name}.json is missing — capture it (CAVNAR_WRITE_WEB_FIXTURES=1)"
        problems += [f"{name}: {m}" for m in shape_mismatches(_load(name), live[name])[:8]]
    assert problems == [], ("a dashboard read changed shape — re-capture (CAVNAR_WRITE_WEB_FIXTURES=1) and check "
                            "the script that reads it:\n" + "\n".join(problems))


def test_no_key_the_open_dashboard_reads_was_removed(live):
    problems = []
    for name in NAMES:
        assert os.path.exists(_path(name + ".v1")), f"{name}.v1.json is missing"
        v1 = _load(name + ".v1")
        problems += [f"{name}: {q} is no longer sent" for q in ios.removed_paths(v1, live[name])[:8]]
        problems += [f"{name}: {m}" for m in shape_mismatches(v1, live[name])[:8]]
    assert problems == [], (
        "a key the dashboard's script reads was removed or changed kind — an open tab renders 'undefined'. Keep "
        "sending it, or change the reader and replace <name>.v1.json in the same commit:\n" + "\n".join(problems))


def test_every_route_is_one_the_dashboard_reads():
    """Guards the guard: each captured path is a literal in the templates."""
    src = ""
    for dirpath, _d, files in os.walk(os.path.join(ROOT, "templates")):
        for f in files:
            if f.endswith(".html"):
                with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                    src += fh.read()
    for name, (_who, path, _q) in ROUTES.items():
        assert f"'{path}" in src or f'"{path}' in src, f"{name}: {path} is not read by any template"
    assert "/api/dsr/week" in src


def test_the_fixtures_are_the_fictional_store_and_carry_no_credential():
    emails = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    for name in NAMES:
        for suffix in ("", ".v1"):
            text = open(_path(name + suffix), encoding="utf-8").read()
            for m in emails.finditer(text):
                assert m.group(1).lower().endswith((".example", ".invalid", ".test", "cavnar.ai")), (name, m.group(0))
            for secret in (r"demo-store:", r"Bearer\s", r"sk_live_", r"\bre_[A-Za-z0-9]{16,}", r"AKIA[0-9A-Z]{16}",
                           r"/staff/cal/[A-Za-z0-9_-]{16,}", r"/join/(?!<volatile>)[A-Za-z0-9_.-]{6,}"):
                assert not re.search(secret, text), (name + suffix, secret)


def test_the_removal_check_catches_a_renamed_dashboard_key():
    v1 = {"ok": True, "profitability": {"prime_cost_pct": 61.2, "labor_pct": 30.1}}
    live = {"ok": True, "profitability": {"prime_pct": 61.2, "labor_pct": 30.1}}
    assert ios.removed_paths(v1, live) == ["$.profitability.prime_cost_pct"]
    assert shape_mismatches(v1, {"ok": True, "profitability": {"prime_cost_pct": "61%", "labor_pct": 30.1}}) == [
        "$.profitability.prime_cost_pct: the fixture has float, the server sent string"]
