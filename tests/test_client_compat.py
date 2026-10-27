"""The iPhone app's build on every request, the minimum build and the 426
update answer, and the decode-failure reports (release-safety audit #25:
A06-02, A06-04, A05-14; client_compat.py).

Today's installed builds send no header: they must keep working until the
cutoff an operator sets, and a browser's staff portal is never mistaken for
an old app."""
import json
import os
import subprocess
import sys
import tempfile
from datetime import date

import pytest
from flask import Flask

import auth
import client_compat as cc
import mobile_api
import models
import ops
from models import Restaurant, create_restaurant

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(**kw):
    return {k: str(v) for k, v in kw.items()}


def _h(app=None, platform="ios", bearer=False):
    headers = {}
    if app is not None:
        headers[cc.APP_HEADER] = app
    if platform is not None:
        headers[cc.PLATFORM_HEADER] = platform
    if bearer:
        headers["Authorization"] = "Bearer tok"
    return headers


# ── the header ──────────────────────────────────────────────────────────────

def test_the_header_reads_version_and_build():
    app = cc.parse_app("1.0 (4210)", "ios")
    assert app == cc.ClientApp("1.0", 4210, "ios") and app.label == "ios 1.0 (4210)"
    assert cc.parse_app(" 2.3.1 ( 77 ) ") == cc.ClientApp("2.3.1", 77, "ios")
    # A platform it does not know is still the app (the only one there is).
    assert cc.parse_app("1.0 (3)", "android").platform == "ios"
    for bad in (None, "", "1.0", "(12)", "1.0 (abc)", "1.0 (12) extra", "x" * 40 + " (1)"):
        assert cc.parse_app(bad) is None, bad


def test_the_settings_read_and_a_bad_value_never_refuses_anyone():
    assert cc.min_build(_env()) is None
    assert cc.min_build(_env(MOBILE_MIN_BUILD="4100")) == 4100
    assert cc.min_build(_env(MOBILE_MIN_BUILD="soon")) is None
    assert cc.min_build(_env(MOBILE_MIN_BUILD="0")) is None
    assert cc.headerless_cutoff(_env(MOBILE_HEADERLESS_CUTOFF="2026-12-01")) == date(2026, 12, 1)
    assert cc.headerless_cutoff(_env(MOBILE_HEADERLESS_CUTOFF="12/1/26")) is None
    assert cc.headerless_cutoff(_env()) is None


# ── who is turned away ──────────────────────────────────────────────────────

TODAY = date(2026, 11, 2)


def _refusal(path, headers, method="GET", **env):
    return cc.update_refusal(path, method, headers, today=TODAY, environ=_env(**env))


def test_with_nothing_set_every_request_is_served():
    assert _refusal("/mobile/api/home", _h("1.0 (1)")) is None
    assert _refusal("/mobile/api/home", _h(None, None)) is None
    assert _refusal("/staff/api/shifts", _h(None, None, bearer=True)) is None


def test_a_build_below_the_minimum_is_told_to_update_on_both_apis():
    for path in ("/mobile/api/home", "/mobile/api/login", "/staff/api/shifts"):
        body = _refusal(path, _h("1.0 (4099)", bearer=True), MOBILE_MIN_BUILD=4100,
                        IOS_APP_STORE_URL="https://apps.apple.com/app/id1")
        assert body == {"ok": False, "update_required": True, "error": cc.UPDATE_MESSAGE,
                        "message": cc.UPDATE_MESSAGE, "min_build": 4100,
                        "app_store_url": "https://apps.apple.com/app/id1"}, path
    assert _refusal("/mobile/api/home", _h("1.0 (4100)"), MOBILE_MIN_BUILD=4100) is None
    assert _refusal("/mobile/api/home", _h("1.1 (5000)"), MOBILE_MIN_BUILD=4100) is None
    # The report route takes a too-old build's report anyway; preflights and
    # the web are never judged.
    assert _refusal(cc.REPORT_PATH, _h("1.0 (1)"), "POST", MOBILE_MIN_BUILD=4100) is None
    assert _refusal("/mobile/api/home", _h("1.0 (1)"), "OPTIONS", MOBILE_MIN_BUILD=4100) is None
    assert _refusal("/api/home/brief", _h("1.0 (1)"), MOBILE_MIN_BUILD=4100) is None


def test_todays_headerless_builds_keep_working_until_the_cutoff():
    old = _h(None, None)
    assert _refusal("/mobile/api/home", old, MOBILE_MIN_BUILD=4100) is None          # no cutoff set
    assert _refusal("/mobile/api/home", old, MOBILE_HEADERLESS_CUTOFF="2026-11-02") is None    # the last day
    body = _refusal("/mobile/api/home", old, MOBILE_HEADERLESS_CUTOFF="2026-11-01")
    assert body["update_required"] is True and body["error"] == cc.UPDATE_MESSAGE
    # The staff app's own calls carry its Bearer token: an old staff build is
    # judged the same way.
    assert _refusal("/staff/api/shifts", _h(None, None, bearer=True),
                    MOBILE_HEADERLESS_CUTOFF="2026-11-01")["update_required"]


def test_the_browser_staff_portal_is_never_an_old_app():
    """/staff/api/* on the PIN cookie, with no header and no Bearer, is the
    browser portal (staff_portal.html): past the cutoff it is still served."""
    assert _refusal("/staff/api/shifts", {"Cookie": "staff_session=x"},
                    MOBILE_HEADERLESS_CUTOFF="2026-01-01", MOBILE_MIN_BUILD=4100) is None
    assert not cc.is_app_traffic("/staff/api/shifts", {})
    assert cc.is_app_traffic("/staff/api/shifts", _h("1.0 (2)"))
    assert cc.is_app_traffic("/mobile/api/me", {})


# ── the gate on a real app ──────────────────────────────────────────────────

def _sweep(monkeypatch, db_path):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)


@pytest.fixture
def world(db_path, monkeypatch):
    _sweep(monkeypatch, db_path)
    for name in ("MOBILE_MIN_BUILD", "MOBILE_HEADERLESS_CUTOFF", "IOS_APP_STORE_URL"):
        monkeypatch.delenv(name, raising=False)
    rid = create_restaurant(Restaurant(name="Compat Grill", owner_email="owner@compat.test"), db_path=db_path)
    uid = auth.create_user(rid, "compatowner", "owner@compat.test", "Compat-pass-2026", db_path=db_path)
    app = Flask(__name__)
    import staff_routes
    import strategy_routes
    for bp in (mobile_api.mobile_bp, strategy_routes.strategy_mobile_bp, staff_routes.staff_bp):
        before = getattr(bp, "_got_registered_once", False)
        app.register_blueprint(bp)
        bp._got_registered_once = before
    cc.install(app)
    cc.install(app)          # idempotent
    return {"db": db_path, "rid": rid, "uid": uid, "client": app.test_client(), "app": app}


def _bearer(world, **kw):
    return auth.create_session(world["uid"], device_type="ios", restaurant_id=world["rid"], db_path=world["db"], **kw)


def test_the_gate_answers_426_before_the_route_runs(world, monkeypatch):
    token = _bearer(world)
    c = world["client"]
    auth_h = {"Authorization": f"Bearer {token}"}
    assert c.get("/mobile/api/me", headers=auth_h).status_code == 200        # nothing set
    monkeypatch.setenv("MOBILE_MIN_BUILD", "4100")
    low = c.get("/mobile/api/me", headers={**auth_h, **_h("1.0 (4000)")})
    assert low.status_code == 426
    assert low.get_json()["update_required"] is True and low.get_json()["min_build"] == 4100
    assert low.headers["Cache-Control"] == "no-store"
    assert c.get("/mobile/api/me", headers={**auth_h, **_h("1.0 (4100)")}).status_code == 200
    # Headerless: served (no cutoff) — then refused past one.
    assert c.get("/mobile/api/me", headers=auth_h).status_code == 200
    monkeypatch.setenv("MOBILE_HEADERLESS_CUTOFF", "2000-01-01")
    assert c.get("/mobile/api/me", headers=auth_h).status_code == 426
    # Sign-in is judged too, so a too-old build is told before it signs in.
    assert c.post("/mobile/api/login", json={"username": "x", "password": "y"},
                  headers=_h("1.0 (3)")).status_code == 426
    # A staff route the app calls with its Bearer token.
    assert c.get("/staff/api/shifts", headers={**auth_h, **_h("1.0 (3)")}).status_code == 426


def test_the_full_app_wires_the_gate():
    """hosted_dashboard installs it: booted in a throwaway volume (no keys,
    no network), a build under MOBILE_MIN_BUILD gets 426 on a real route and
    a headerless request reaches the route's own answer."""
    def boot(force):
        vol = tempfile.mkdtemp(prefix="cavnar-compat-")
        out = subprocess.run([sys.executable, "-c", _BOOT_SCRIPT, vol, force], cwd=ROOT, capture_output=True,
                             text=True, timeout=180)
        line = [l for l in out.stdout.splitlines() if l.startswith("RESULT ")]
        assert out.returncode == 0 and line, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
        return json.loads(line[-1][len("RESULT "):]), out.stdout + out.stderr
    # MOBILE_MIN_BUILD_FORCE: the minimum as set (a fresh volume has no
    # session that ever named a build).
    got, _err = boot("1")
    assert got == {"low": 426, "ok": 401, "headerless": 401, "web": 302, "ignored": False}, got
    # Unforced, the boot finds no installed build that could meet it: ignored,
    # loudly, and nobody is turned away (blind re-audit 10/27/26).
    got, err = boot("")
    assert got == {"low": 401, "ok": 401, "headerless": 401, "web": 302, "ignored": True}, got
    assert "MOBILE_MIN_BUILD=4100 is ignored" in err, err[-2000:]


_BOOT_SCRIPT = r'''
import json, os, socket, sqlite3, sys
vol = sys.argv[1]
os.environ["MOBILE_MIN_BUILD_FORCE"] = sys.argv[2]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", STRIPE_SECRET_KEY="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0",
                  GOOGLE_PLACES_API_KEY="", PERPLEXITY_API_KEY="", OPENAI_API_KEY="", MOBILE_MIN_BUILD="4100")
os.environ.pop("RAILWAY_ENVIRONMENT", None); os.environ.pop("RAILWAY_PROJECT_ID", None)
os.environ.pop("DATABASE_URL", None)
def _blocked(*a, **k):
    raise OSError("network blocked in test subprocess")
socket.create_connection = _blocked
socket.socket.connect = lambda self, *a, **k: _blocked()
import dotenv; dotenv.load_dotenv = lambda *a, **k: False  # never production's .env (audit #25)
import hosted_dashboard as h
c = h.app.test_client()
out = {
    "low": c.get("/mobile/api/me", headers={"X-Cavnar-App": "1.0 (4000)", "X-Cavnar-Platform": "ios"}).status_code,
    "ok": c.get("/mobile/api/me", headers={"X-Cavnar-App": "1.0 (4100)", "X-Cavnar-Platform": "ios"}).status_code,
    "headerless": c.get("/mobile/api/me").status_code,
    "web": c.get("/").status_code,
    "ignored": bool(h.client_compat._MIN_BUILD_CHECK.get("ignored")),
}
print("RESULT " + json.dumps(out))
'''


# ── which build each login runs ─────────────────────────────────────────────

def _client_app(db, table, where, arg):
    conn = models.get_conn(db)
    try:
        return conn.execute(f"SELECT client_app FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT 1",
                            (arg,)).fetchone()[0]
    finally:
        conn.close()


def test_a_sign_in_keeps_the_build_it_came_from(world):
    with world["app"].test_request_context("/mobile/api/login", headers=_h("1.0 (4210)")):
        token = _bearer(world)
    assert _client_app(world["db"], "sessions", "token=?", auth.hash_session_token(token)) == "ios 1.0 (4210)"
    assert _client_app(world["db"], "login_history", "user_id=?", world["uid"]) == "ios 1.0 (4210)"
    # A browser sign-in (no header) keeps nothing.
    with world["app"].test_request_context("/login"):
        web = auth.create_session(world["uid"], db_path=world["db"])
    assert _client_app(world["db"], "sessions", "token=?", auth.hash_session_token(web)) is None


def test_an_upgrade_without_a_new_sign_in_is_seen_on_the_next_touch(world):
    with world["app"].test_request_context("/mobile/api/login", headers=_h("1.0 (4210)")):
        token = _bearer(world)
    conn = models.get_conn(world["db"])
    conn.execute("UPDATE sessions SET last_active=datetime('now', '-5 minutes') WHERE token=?",
                 (auth.hash_session_token(token),))
    conn.commit()
    conn.close()
    r = world["client"].get("/mobile/api/me", headers={"Authorization": f"Bearer {token}", **_h("1.1 (4300)")})
    assert r.status_code == 200
    assert _client_app(world["db"], "sessions", "token=?", auth.hash_session_token(token)) == "ios 1.1 (4300)"
    # A request with no header leaves the build the session last reported.
    conn = models.get_conn(world["db"])
    conn.execute("UPDATE sessions SET last_active=datetime('now', '-5 minutes') WHERE token=?",
                 (auth.hash_session_token(token),))
    conn.commit()
    conn.close()
    world["client"].get("/mobile/api/me", headers={"Authorization": f"Bearer {token}"})
    assert _client_app(world["db"], "sessions", "token=?", auth.hash_session_token(token)) == "ios 1.1 (4300)"


def test_the_installed_builds_are_read_from_live_sessions(world, monkeypatch):
    for label in ("1.0 (4210)", "1.0 (4210)", "1.1 (4300)"):
        with world["app"].test_request_context("/mobile/api/login", headers=_h(label)):
            _bearer(world)
    _bearer(world)                                       # an old build: no header
    monkeypatch.setenv("MOBILE_MIN_BUILD", "4300")
    got = cc.installed_builds(world["db"])
    assert [(b["client_app"], b["sessions"]) for b in got["builds"]] == [("ios 1.1 (4300)", 1),
                                                                          ("ios 1.0 (4210)", 2)]
    assert got["headerless_sessions"] == 1 and got["below_minimum"] == 2 and got["min_build"] == 4300


# ── decode-failure reports ──────────────────────────────────────────────────

REPORT = {"route": "/mobile/api/dsr/2026-10-25?peek=1", "method": "GET", "status": 200,
          "coding_path": "facts.blocks.sales.metrics.net", "kind": "decoding",
          "description": "Expected to decode Double but found a string instead.", "device_id": "DEVICE-A"}


def _rows(db):
    conn = models.get_conn(db)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM client_error_reports ORDER BY id")]
    finally:
        conn.close()


def test_a_report_is_stored_with_its_route_path_and_build_never_a_body(world):
    token = _bearer(world)
    r = world["client"].post("/mobile/api/client-errors", json=REPORT,
                             headers={"Authorization": f"Bearer {token}", **_h("1.0 (4210)")})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "stored": True}
    (row,) = _rows(world["db"])
    assert row["route"] == "/mobile/api/dsr/:id"                 # the id and the query are not the route
    assert row["coding_path"] == REPORT["coding_path"] and row["http_status"] == 200
    assert row["client_app"] == "ios 1.0 (4210)" and row["app_build"] == 4210 and row["platform"] == "ios"
    assert row["restaurant_id"] == world["rid"] and row["user_id"] == world["uid"]
    assert "DEVICE-A" not in json.dumps(row)                     # the device id is kept only as a hash
    assert row["kind"] == "decoding" and row["method"] == "GET"


def test_a_report_needs_no_session_and_a_dead_one_never_signs_the_phone_out(world):
    r = world["client"].post("/mobile/api/client-errors", json=REPORT,
                             headers={"Authorization": "Bearer not-a-session"})
    assert r.status_code == 200
    (row,) = _rows(world["db"])
    assert row["restaurant_id"] is None and row["user_id"] is None


def test_a_route_the_server_has_no_rule_for_is_refused(world):
    """A report names a route the server really has (blind re-audit
    10/27/26): made-up routes were each a new page key."""
    for route in ("/mobile/api/CALL-555-0100-NOW", "/mobile/api/zz1", "/staff/api/not-a-thing"):
        r = world["client"].post("/mobile/api/client-errors", json=dict(REPORT, route=route))
        assert r.status_code == 400, route
    assert _rows(world["db"]) == []
    # A real rule answers, whatever method the report says (a 405 path is a route).
    for route, method in (("/mobile/api/home", "GET"), ("/mobile/api/home", "POST"),
                          ("/staff/api/shifts", "GET"), ("/mobile/api/dsr/2026-10-25?x=1", "GET")):
        assert cc.known_route(route, method, app=world["app"]), route
    assert not cc.known_route("/api/home/brief", "GET", app=world["app"])
    assert cc.known_route("/mobile/api/anything", "GET") is True        # outside an app nothing is checked


def test_anonymous_reports_have_their_own_smaller_budget(world, monkeypatch):
    c = world["client"]
    monkeypatch.setattr(cc, "ANON_IP_HOURLY", 2)
    for i in range(2):
        assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id=f"A{i}")).status_code == 200
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="A9")).status_code == 429
    # A signed-in report from the same address is not held by the anonymous cap.
    token = _bearer(world)
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="S1"),
                  headers={"Authorization": f"Bearer {token}"}).status_code == 200
    monkeypatch.setattr(cc, "ANON_IP_HOURLY", 100)
    monkeypatch.setattr(cc, "ANON_GLOBAL_HOURLY", 3)
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="A3")).status_code == 200
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="A4")).status_code == 429
    # A signed-in user has a budget of its own, whatever device ids it sends.
    monkeypatch.setattr(cc, "USER_HOURLY", 1)
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="S2", route="/mobile/api/home"),
                  headers={"Authorization": f"Bearer {token}"}).status_code == 429


def test_the_same_failure_within_the_hour_is_one_row_and_a_device_is_capped(world, monkeypatch):
    c = world["client"]
    for _ in range(3):
        assert c.post("/mobile/api/client-errors", json=REPORT).status_code == 200
    (row,) = _rows(world["db"])
    assert row["repeats"] == 3
    monkeypatch.setattr(cc, "DEVICE_HOURLY", 2)
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, route="/mobile/api/home")).status_code == 200
    capped = c.post("/mobile/api/client-errors", json=dict(REPORT, route="/mobile/api/labor"))
    assert capped.status_code == 429 and capped.get_json()["rate_limited"] is True
    # Another device is its own budget.
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="DEVICE-B")).status_code == 200
    monkeypatch.setattr(cc, "GLOBAL_HOURLY", 3)
    assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="DEVICE-C")).status_code == 429


def test_a_report_about_anything_but_the_apps_routes_is_refused(world):
    for route in ("/api/home", "https://evil.example/x", "", None):
        r = world["client"].post("/mobile/api/client-errors", json=dict(REPORT, route=route))
        assert r.status_code == 400, route
    assert world["client"].post("/mobile/api/client-errors", data="[1]",
                                content_type="application/json").status_code == 400
    assert _rows(world["db"]) == []


def test_a_report_is_clipped_and_an_unknown_kind_or_method_is_not_kept_as_sent(world):
    long = dict(REPORT, coding_path="x." * 400, description="y" * 2000, kind="crash", method="TRACE",
                status="abc")
    assert world["client"].post("/mobile/api/client-errors", json=long).status_code == 200
    (row,) = _rows(world["db"])
    assert len(row["coding_path"]) == cc.MAX_PATH and len(row["description"]) == cc.MAX_DESCRIPTION
    assert row["kind"] == "decoding" and row["method"] is None and row["http_status"] is None


def test_the_route_is_ungated_by_module_and_reached_before_any_sign_in():
    assert "/mobile/api/client-errors" in auth._UNGATED_PREFIXES
    assert cc.REPORT_PATH in cc.EXEMPT_PATHS


def _users(world, n):
    """`n` signed-in logins of the restaurant, each with its own session."""
    tokens = []
    for i in range(n):
        uid = auth.create_user(world["rid"], f"compatmgr{i}", f"mgr{i}@compat.test", "Compat-pass-2026",
                               db_path=world["db"])
        tokens.append(auth.create_session(uid, device_type="ios", restaurant_id=world["rid"], db_path=world["db"]))
    return tokens


def test_anonymous_reports_never_page_however_many_device_ids_they_send(world, monkeypatch):
    """Four POSTs from one address with four made-up device ids paged the
    operator (blind re-audit 10/27/26). Only signed-in users count."""
    c = world["client"]
    for i in range(6):
        assert c.post("/mobile/api/client-errors", json=dict(REPORT, device_id=f"attacker-{i}"),
                      headers=_h("1.0 (4210)")).status_code == 200
    surge = cc.decode_failure_surge(threshold=3, db_path=world["db"])
    assert surge["devices"] == 6 and surge["anonymous_devices"] == 6
    assert surge["users"] == 0 and surge["over"] is False
    assert cc.surge_problem(surge) is None
    # One signed-in user with many device ids is still one user.
    token = _bearer(world)
    for i in range(5):
        c.post("/mobile/api/client-errors", json=dict(REPORT, device_id=f"same-user-{i}"),
               headers={"Authorization": f"Bearer {token}"})
    assert cc.decode_failure_surge(threshold=3, db_path=world["db"])["users"] == 1
    monkeypatch.setattr(cc, "ALERT_USERS", 3)
    out = ops.check_platform_sla(send=False, db_path=world["db"], write_ok=True)
    assert out["client_decode"]["over"] is False
    assert not any("could not read the server's answer" in p for p in out["problems"])


def test_more_signed_in_users_than_the_threshold_is_one_fixed_line_with_counts(world, monkeypatch):
    c = world["client"]
    evil = dict(REPORT, coding_path="$.Your account is locked. Text 555-0100 to restore it",
                description="Text 555-0100 now")
    for i, token in enumerate(_users(world, 4)):
        assert c.post("/mobile/api/client-errors", json=dict(evil, device_id=f"D{i}"),
                      headers={"Authorization": f"Bearer {token}", **_h("1.0 (4210)")}).status_code == 200
    surge = cc.decode_failure_surge(threshold=3, db_path=world["db"])
    assert surge["users"] == 4 and surge["restaurants"] == 1 and surge["over"] is True
    assert surge["routes"][0]["route"] == "/mobile/api/dsr/:id" and surge["routes"][0]["users"] == 4
    line = cc.surge_problem(surge)
    assert "4 signed-in users at 1 restaurant" in line and "on 1 route" in line
    # Never the client's words: no route, coding path or description.
    for reported in ("/mobile/api/dsr", "555-0100", "locked", "Text"):
        assert reported not in line, reported
    assert cc.surge_problem(cc.decode_failure_surge(threshold=10, db_path=world["db"])) is None
    monkeypatch.setattr(cc, "ALERT_USERS", 3)
    out = ops.check_platform_sla(send=False, db_path=world["db"], write_ok=True)
    assert out["client_decode"]["over"] is True
    assert any("could not read the server's answer for 4 signed-in users" in p for p in out["problems"])


def test_the_page_key_is_fixed_whatever_routes_are_reported(world, monkeypatch):
    """A new route string used to be a new warn key, past the 24-hour
    cooldown: the kind is now the fixed `client_decode`."""
    c = world["client"]
    tokens = _users(world, 4)
    monkeypatch.setattr(cc, "ALERT_USERS", 3)
    kinds = []
    for route in ("/mobile/api/home", "/mobile/api/labor", "/staff/api/shifts"):
        for i, token in enumerate(tokens):
            c.post("/mobile/api/client-errors", json=dict(REPORT, route=route, device_id=f"{route}{i}"),
                   headers={"Authorization": f"Bearer {token}"})
        out = ops.check_platform_sla(send=False, db_path=world["db"], write_ok=True)
        kinds.append([k for k in out["warn_kinds"] if "client_decode" in k])
    assert kinds == [["client_decode"]] * 3, kinds


def test_the_reports_are_a_registered_ledger():
    assert ops._RETENTION_DAYS["client_error_reports"] == 30
    assert ops._RETENTION_COLUMN["client_error_reports"] == "created_at"
    assert ops._RETENTION_FLOOR_DAYS["client_error_reports"] <= 30


def test_the_report_table_is_created_at_boot(db_path):
    conn = models.get_conn(db_path)
    try:
        conn.execute("SELECT id, device_key, route, coding_path, repeats FROM client_error_reports LIMIT 1")
        conn.execute("SELECT client_app FROM sessions LIMIT 1")
        conn.execute("SELECT client_app FROM login_history LIMIT 1")
    finally:
        conn.close()


def test_the_bug_report_names_the_build_the_request_came_from(world, monkeypatch):
    import admin_events
    stored = {}
    monkeypatch.setattr(admin_events, "store_bug_report",
                        lambda rid, user, message, meta, source: stored.update(meta) or 1)
    monkeypatch.setattr(admin_events, "mark_bug_report_notified", lambda *a, **k: None)
    token = _bearer(world)
    r = world["client"].post("/mobile/api/account/report-bug", json={"message": "The Labor tab is blank."},
                             headers={"Authorization": f"Bearer {token}", **_h("1.0 (4210)")})
    assert r.status_code == 200
    assert stored["client_app"] == "ios 1.0 (4210)"


# ── a MOBILE_MIN_BUILD nothing installed can meet (blind re-audit 10/27/26) ─

def test_a_minimum_above_every_installed_build_is_ignored_unless_forced():
    assert cc.min_build_verdict(4300, 4300) == (4300, None)
    assert cc.min_build_verdict(4100, 4300) == (4100, None)
    enforced, why = cc.min_build_verdict(43000, 4300)          # a digit too many
    assert enforced is None and "43000" in why and "4300" in why and "MOBILE_MIN_BUILD_FORCE" in why
    assert cc.min_build_verdict(4350, 4300, margin=100) == (4350, None)
    assert cc.min_build_verdict(43000, 4300, force=True) == (43000, None)
    # No session ever sent a build: nothing shows the minimum can be met.
    enforced, why = cc.min_build_verdict(4100, None)
    assert enforced is None and "no live session" in why
    assert cc.min_build_verdict(None, 4300) == (None, None)


def test_the_check_reads_the_installed_builds_and_the_gate_stops_refusing(world, monkeypatch):
    with world["app"].test_request_context("/mobile/api/login", headers=_h("1.0 (4210)")):
        token = _bearer(world)
    assert cc.newest_seen_build(world["db"]) == 4210
    # The process's own database: what the gate and the platform check read.
    monkeypatch.setattr(models, "DB_PATH", world["db"])
    monkeypatch.setattr(cc, "_MIN_BUILD_CHECK", {})
    monkeypatch.setenv("MOBILE_MIN_BUILD", "42100")
    verdict = cc.check_min_build()
    assert verdict["ignored"] is True and verdict["newest"] == 4210 and cc.min_build() is None
    assert "42100" in cc.min_build_problem()
    c = world["client"]
    r = c.get("/mobile/api/me", headers={"Authorization": f"Bearer {token}", **_h("1.0 (4210)")})
    assert r.status_code == 200, "an ignored minimum refuses nobody"
    # The platform check names it (one fixed kind) and re-checks it.
    out = ops.check_platform_sla(send=False, write_ok=True)
    assert "mobile_min_build" in out["warn_kinds"] and any("42100" in p for p in out["problems"])
    assert any("42100" in w for w in __import__("platform_monitor").system_warnings(
        {"mobile_clients": {"installed": cc.installed_builds(world["db"])}}))
    # Forced: enforced as set.
    monkeypatch.setenv("MOBILE_MIN_BUILD_FORCE", "1")
    assert cc.check_min_build()["ignored"] is False and cc.min_build() == 42100
    assert c.get("/mobile/api/me", headers={"Authorization": f"Bearer {token}", **_h("1.0 (4210)")}).status_code == 426
    # A sane minimum is enforced once checked.
    monkeypatch.delenv("MOBILE_MIN_BUILD_FORCE")
    monkeypatch.setenv("MOBILE_MIN_BUILD", "4210")
    assert cc.check_min_build()["ignored"] is False and cc.min_build() == 4210 and cc.min_build_problem() is None
    # A verdict read from another database never decides for the process.
    monkeypatch.setenv("MOBILE_MIN_BUILD", "99999")
    monkeypatch.setattr(cc, "_MIN_BUILD_CHECK", {"raw": 99999, "ignored": True, "db": "/elsewhere.db", "why": "x"})
    assert cc.min_build() == 99999 and cc.min_build_problem() is None


def test_the_smoke_build_is_never_kept_as_an_installed_one(world):
    with world["app"].test_request_context("/mobile/api/me", headers={"X-Cavnar-App": "smoke (999999999)",
                                                                       "X-Cavnar-Platform": "ios"}):
        assert cc.request_client_app() is None
        token = _bearer(world)
    assert _client_app(world["db"], "sessions", "token=?", auth.hash_session_token(token)) is None
    assert cc.newest_seen_build(world["db"]) is None
    # ...and its build is above any minimum, so the gate serves it.
    assert _refusal("/mobile/api/me", {"X-Cavnar-App": "smoke (999999999)", "X-Cavnar-Platform": "ios"},
                    MOBILE_MIN_BUILD=4100, MOBILE_HEADERLESS_CUTOFF="2000-01-01") is None


# ── compatibility telemetry ─────────────────────────────────────────────────

def test_the_console_counts_headerless_sessions_and_reports_per_build(world):
    for label in ("1.0 (4210)", "1.1 (4300)"):
        with world["app"].test_request_context("/mobile/api/login", headers=_h(label)):
            _bearer(world)
    _bearer(world)
    _bearer(world)                                         # two builds from before the header
    got = cc.installed_builds(world["db"])
    assert got["header_sessions"] == 2 and got["headerless_sessions"] == 2 and got["headerless_share_pct"] == 50.0
    c = world["client"]
    token = _bearer(world)
    c.post("/mobile/api/client-errors", json=REPORT, headers={"Authorization": f"Bearer {token}", **_h("1.1 (4300)")})
    c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="X", route="/mobile/api/home"),
           headers=_h("1.1 (4300)"))
    c.post("/mobile/api/client-errors", json=dict(REPORT, device_id="Y"))
    by = {r["client_app"]: r for r in cc.decode_reports_by_build(db_path=world["db"])}
    assert by["ios 1.1 (4300)"]["reports"] == 2 and by["ios 1.1 (4300)"]["routes"] == 2
    assert by["ios 1.1 (4300)"]["users"] == 1
    assert by["no build header"]["reports"] == 1
