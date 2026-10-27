"""The suite in production's configuration (audit #25 A01-10).

tests/conftest.py flips switches production never flips, so ~3,000 schedule
tests run the model arm (production drafts with the solver), Home builds a
missing block on the request (production queues it), the per-login request
share is off, the password policy and the breach check are off, the send
gate's reviewer is forced to pass, and a generation's state is strict. A
regression only production's configuration reaches passed all of them.

This file undoes every one of those switches (`production_config`) and runs
the paths they guard, once each: a solver generation polled to done, a Home
block queued then served, a login under the password policy and two-factor,
and a burst that meets the request share. `test_every_switch_the_suite_
flips_is_undone_here` keeps the list honest when conftest gains a switch.
"""
import datetime as dt
import hashlib
import os
import re

import pytest
from flask import Flask

import ai_reviewer
import auth
import auth_routes
import models
from models import Restaurant, create_restaurant, update_restaurant

pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = "prod-smoke-csrf"

# The real send-gate reviewer, captured before conftest's autouse fixture
# replaces it for each test.
_REAL_GATE_REVIEW = ai_reviewer._gate_review

# What production runs with: None = unset (the code's default).
PRODUCTION = {
    "SCHEDULE_DRAFT_SOURCE": None,      # the solver drafts (schedule_fallback.DEFAULT_SOURCE)
    "HOME_SNAPSHOT_COLD": None,         # a missing Home block is queued, the route answers `preparing`
    "OWNER_RULES_READ": None,           # a generation reads the owner's words with a model
    "SCHEDULE_NOTE_WRITE": None,        # and writes the week's note
    "AI_SHADOW_REVIEW": None,           # the orchestrator's shadow reviewer samples passing runs
    "LEARNING_HOLDOUT_PCT": None,       # the learning holdout is on
    "REQUEST_LIMITS": None,             # the per-login and per-restaurant request share is on
    "ADMIN_REQUIRE_2FA": "1",           # set on production (docs/ops/SECURITY.md: after enrolment)
    "HIBP_DISABLED": None,              # the breach check runs
    "SCHEDULE_STATE_STRICT": None,      # an undeclared state key reads None instead of raising
}
# What conftest sets that changes no behaviour production has: the volume and
# seasons a test reads, a key and pepper of its own, the CAN-SPAM address
# production also sets, and blank provider keys (never a real send).
HARMLESS = {"RAILWAY_VOLUME_MOUNT_PATH", "CAVNAR_SEASONS_DIR", "SECRET_KEY", "CAVNAR_PIN_PEPPER",
            "CAVNAR_POSTAL_ADDRESS", "RESEND_API_KEY", "TWILIO_AUTH_TOKEN", "STRIPE_SECRET_KEY",
            # pytest-timeout's ceiling: read by the test runner, never by the app.
            "PYTEST_TIMEOUT"}


@pytest.fixture
def production_config(monkeypatch):
    for key, value in PRODUCTION.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)
    monkeypatch.setattr(ai_reviewer, "_gate_review", _REAL_GATE_REVIEW)
    # The breach check runs for real; only its answer from the network is
    # canned (nothing breached unless a test says otherwise).
    import requests
    real_get = requests.get

    class _NoBreach:
        status_code = 200
        text = "0000000000000000000000000000000000A:1\n"
    monkeypatch.setattr(requests, "get", lambda url, *a, **k: _NoBreach() if "pwnedpasswords.com/range/" in str(url)
                        else real_get(url, *a, **k))
    import request_limits
    request_limits.reset()
    yield
    request_limits.reset()


def environment_writes(source):
    """Every environment variable `source` sets, however it is written (blind
    re-audit 10/27/26 B1 F17: the regexes saw only three literal forms):
    os.environ.setdefault / os.environ[...] = / os.environ.update({...} or
    KEY=...) / os.putenv / monkeypatch.setenv, a name held in a constant
    included, and every key of a dict a loop then writes with
    os.environ[key] = value."""
    import ast
    tree = ast.parse(source)
    consts = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)
              and isinstance(n.value.value, str)}

    def name(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in consts:
            return consts[node.id]
        return None

    def is_environ(node):
        return isinstance(node, ast.Attribute) and node.attr == "environ"
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            f = n.func
            if f.attr in ("setdefault", "update") and is_environ(f.value) or f.attr in ("setenv", "putenv"):
                if f.attr == "update":
                    for arg in n.args:
                        if isinstance(arg, ast.Dict):
                            out |= {name(k) for k in arg.keys if name(k)}
                    out |= {k.arg for k in n.keywords if k.arg}
                elif n.args and name(n.args[0]):
                    out.add(name(n.args[0]))
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and is_environ(t.value) and name(t.slice):
                    out.add(name(t.slice))
    return {v for v in out if v and v.isupper()}


def test_the_environment_scan_reads_every_way_of_writing_a_variable():
    src = ("import os\nKEY = 'ABC_ONE'\n"
           "os.environ.setdefault('A_TWO', '1')\nos.environ['A_THREE'] = '1'\nos.environ[KEY] = '1'\n"
           "os.environ.update({'A_FOUR': '1'}, A_FIVE='1')\nos.putenv('A_SIX', '1')\n"
           "def t(monkeypatch):\n    monkeypatch.setenv('A_SEVEN', '1')\n")
    assert environment_writes(src) == {"ABC_ONE", "A_TWO", "A_THREE", "A_FOUR", "A_FIVE", "A_SIX", "A_SEVEN"}


def test_every_switch_the_suite_flips_is_undone_here():
    with open(os.path.join(ROOT, "tests", "conftest.py"), encoding="utf-8") as fh:
        text = fh.read()
    flipped = environment_writes(text)
    unexplained = flipped - set(PRODUCTION) - HARMLESS
    assert not unexplained, ("conftest sets %s: add each to PRODUCTION (production's value) or HARMLESS "
                             "(with the reason) here" % sorted(unexplained))
    assert 'monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", False)' in text   # undone by the fixture
    assert "ai_reviewer, \"_gate_review\"" in text                                 # undone by the fixture


def test_production_config_is_what_the_fixture_gives(production_config):
    import request_limits
    import schedule_fallback
    for key, value in PRODUCTION.items():
        assert os.environ.get(key) == value, key
    assert request_limits.enabled()
    assert schedule_fallback.draft_source(1, []) == "solver"
    assert auth.admin_two_factor_required()
    assert ai_reviewer._gate_review is _REAL_GATE_REVIEW


# ── one solver generation, polled to done ───────────────────────────────────

def _route_db(db_path, monkeypatch):
    import sys
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _timed_history():
    """Four weeks of the contract harness's team with their shift times —
    what a POS sync stores, and what the solver reads staffing needs from."""
    import labor
    from test_ios_schedule_payload_contract import MONDAY
    plan = [("Max", "General Manager", "10:00am", "4:00pm"), ("Kim", "General Manager", "4:00pm", "10:30pm"),
            ("Ana", "Server", "11:00am", "4:00pm"), ("Bo", "Server", "4:00pm", "10:00pm"),
            ("Cara", "Server", "4:00pm", "10:00pm"), ("Cy", "Line Cook", "10:00am", "4:00pm"),
            ("Di", "Line Cook", "4:00pm", "10:00pm"), ("Ed", "Bartender", "5:00pm", "11:00pm")]
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    for w in range(1, 5):
        monday = MONDAY - dt.timedelta(weeks=w)
        for i in range(7):
            d = monday + dt.timedelta(days=i)
            lines += [f"{d.isoformat()},{d.strftime('%A')},{n},{r},{a},{b},6,6,900," for n, r, a, b in plan]
    return labor.load_shifts(csv_string="\n".join(lines) + "\n")


def _generate(monkeypatch, db, history=None):
    """A real generation through the job runner, polled as the phone polls:
    (status payload, history detail or None, whether the model was asked to
    draft). The contract harness: the team, ratings, a staff note, a time-off
    request, the Halloween week; the model's answer stubbed."""
    import labor
    import ops
    import schedule_engine as se
    import schedule_prompt
    from test_ios_schedule_payload_contract import _client, _harness
    rid = _harness(monkeypatch, db)
    if history is not None:
        monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda r: history)
    drafted = []
    answer = labor.create_with_retry

    def create(client, **kw):
        if schedule_prompt.request_dates(kw["messages"][0]["content"]):
            drafted.append(1)
        return answer(client, **kw)
    monkeypatch.setattr(labor, "create_with_retry", create)
    client, headers = _client(db, rid)
    ops.start_async_job("prod-smoke-job", "schedule", rid)
    se._run_schedule_job("prod-smoke-job", rid)
    status = client.get("/mobile/api/labor/schedule-status/prod-smoke-job", headers=headers)
    assert status.status_code == 200, status.get_data(as_text=True)
    done = status.get_json()
    detail = None
    if done.get("history_id"):
        detail = client.get(f"/mobile/api/labor/schedule-history/{done['history_id']}", headers=headers)
        assert detail.status_code == 200, detail.get_data(as_text=True)
        detail = detail.get_json()
    return done, detail, bool(drafted)


def test_a_generation_drafts_with_the_solver_and_finishes(production_config, monkeypatch, db_path):
    db = _route_db(db_path, monkeypatch)
    done, detail, model_drafted = _generate(monkeypatch, db, history=_timed_history())
    assert done["ok"] is True and done["status"] == "done", done.get("error")
    assert not model_drafted, "production drafts with the solver; the model was asked to draft"
    assert detail is not None and detail.get("ok") is not False
    # The saved week, judged by the rule engine on production's arm (blind
    # re-audit 10/27/26 B4: the suite's ~3,000 generation tests run the model
    # arm): no legal breach, and no minute with staff on and no manager.
    import csv
    import io
    import schedule_rules as sr
    rows = list(csv.DictReader(io.StringIO(detail["schedule_csv"])))
    assert rows, "the solver drafted an empty week"
    monday = dt.date.fromisoformat(detail["week_start"])
    week = [(monday + dt.timedelta(days=i)).isoformat() for i in range(7)]
    c = sr.build_constraints(detail["restaurant_id"], week, [dt.date.fromisoformat(d).strftime("%A") for d in week])
    legal = [v for v in sr.violations(rows, c) if v.get("hard") and sr.tier_of(v) == sr.TIER_LEGAL]
    assert not legal, legal[:3]
    assert c.managers and not sr.manager_gaps(rows, c), sr.manager_gaps(rows, c)


def test_a_week_with_nothing_to_build_from_says_so_and_never_blames_the_model(production_config, monkeypatch,
                                                                              db_path):
    # The harness's own history has no shift times: no staffing need can be
    # read, and there is no earlier week. The solver (production's source)
    # drafts nothing — and the owner used to read "the model left them empty
    # even when asked again" about a model nobody asked.
    db = _route_db(db_path, monkeypatch)
    done, _detail, model_drafted = _generate(monkeypatch, db)
    assert done["ok"] is False and not model_drafted
    assert "no staffing needs could be read" in done["error"] and "the model" not in done["error"], done["error"]


# ── one Home load: queued, then served ──────────────────────────────────────

def test_a_cold_home_block_is_queued_and_the_next_load_serves_it(production_config, monkeypatch, db_path):
    import good_news
    import job_queue
    from strategy_routes import strategy_bp, strategy_mobile_bp
    db = _route_db(db_path, monkeypatch)
    auth.init_auth(db_path=db)
    monkeypatch.setenv("HOME_SNAPSHOTS", "1")
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)      # the row waits for the queue worker
    rid = create_restaurant(Restaurant(name="Smoke Home", owner_email="home@smoke.test", module_reviews=1),
                            db_path=db)
    uid = auth.create_user(rid, "homeowner", "home@smoke.test", "Smoke-home-pass-2026", db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(uid, db_path=db))
    cold = c.get("/api/good-news")
    assert cold.status_code == 200 and cold.get_json()["preparing"] is True, cold.get_data(as_text=True)[:300]
    conn = models.get_conn(db)
    try:
        queued = conn.execute("SELECT restaurant_id FROM jobs WHERE kind='home_snapshot' AND state='queued'").fetchall()
    finally:
        conn.close()
    assert [r[0] for r in queued] == [rid]
    # The queue worker claims and runs the row, as production's does.
    monkeypatch.setattr(good_news, "all_good_news", lambda *a, **k: [{"key": "r", "headline": "A record night"}])
    assert job_queue.drain(["home"], "prod-smoke-worker", max_jobs=5) == 1
    warm = c.get("/api/good-news").get_json()
    assert "preparing" not in warm and warm["items"][0]["headline"] == "A record night"


# ── one login under the password policy and two-factor ──────────────────────

def _login_app():
    import admin_routes
    import client_api
    import mobile_api
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "prod-smoke"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp, mobile_api.mobile_bp):
        a.register_blueprint(bp)
    return a


def _web_login(c, username, password):
    page = c.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return c.post("/login", data={"username": username, "password": password, "csrf_token": tok})


def test_logins_meet_the_password_policy_and_two_factor(production_config, monkeypatch, db_path):
    import emails
    import notify
    import requests
    db = _route_db(db_path, monkeypatch)
    auth.init_auth(db_path=db)
    models.init_two_fa_backup_codes(db_path=db)
    models.init_email_log(db_path=db)
    auth_routes._login_attempts.clear()
    breached = "Breached-password-2026"
    digest = hashlib.sha1(breached.encode()).hexdigest().upper()

    class _Range:
        status_code = 200
        text = f"0000000000000000000000000000000000A:1\n{digest[5:]}:42\n"
    real_get = requests.get
    monkeypatch.setattr(requests, "get", lambda url, *a, **k: _Range() if "pwnedpasswords.com/range/" in str(url)
                        else real_get(url, *a, **k))
    sent = []
    monkeypatch.setattr(emails, "send_2fa_code", lambda to, name, code, owner=None, **k: sent.append(to) or True)
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: sent.append(to) or True)
    rid = create_restaurant(Restaurant(name="Smoke Login", owner_email="login@smoke.test"), db_path=db)
    # The policy: too short, then breached (the real check, its answer canned).
    with pytest.raises(auth.PasswordPolicyError):
        auth.create_user(rid, "shorty", "s@smoke.test", "short", db_path=db)
    with pytest.raises(auth.PasswordPolicyError):
        auth.create_user(rid, "breached", "b@smoke.test", breached, db_path=db)
    auth.create_user(rid, "owner", "login@smoke.test", "Smoke-login-pass-2026", db_path=db)
    app = _login_app()
    # A restaurant with two-factor on: the password alone is not a session.
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db)
    c = app.test_client()
    c.set_cookie("csrf_js", CSRF)
    r = _web_login(c, "owner", "Smoke-login-pass-2026")
    assert r.status_code == 200 and 'name="pending_token"' in r.get_data(as_text=True)
    assert c.get_cookie("session_token") is None and sent == ["login@smoke.test"]
    # An internal login without a second factor, with production's
    # ADMIN_REQUIRE_2FA=1: signed in, and sent to enrol before the console.
    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="ops@smoke.test"), db_path=db)
    auth.create_user(home, "opsadmin", "ops@smoke.test", "Smoke-admin-pass-2026!", is_admin=True, db_path=db)
    a = app.test_client()
    a.set_cookie("csrf_js", CSRF)
    assert _web_login(a, "opsadmin", "Smoke-admin-pass-2026!").status_code == 302
    page = a.get("/admin")
    assert page.status_code == 302 and "/admin/two-factor" in page.headers["Location"]
    auth_routes._login_attempts.clear()


# ── one burst that meets the request share ──────────────────────────────────

def test_a_burst_from_one_login_meets_the_request_share(production_config, monkeypatch, db_path):
    import client_api
    import request_limits
    db = _route_db(db_path, monkeypatch)
    auth.init_auth(db_path=db)
    rid = create_restaurant(Restaurant(name="Smoke Burst", owner_email="burst@smoke.test"), db_path=db)
    uid = auth.create_user(rid, "burster", "burst@smoke.test", "Smoke-burst-pass-2026", db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(uid, db_path=db))
    capacity = int(request_limits.SESSION_BUCKET[0])
    codes = []
    for _ in range(capacity * 2):
        r = c.get("/api/changelog")
        codes.append(r.status_code)
        if r.status_code == 429:
            assert int(r.headers["Retry-After"]) >= 1
            break
    assert codes[0] == 200, codes[:3]
    assert codes[-1] == 429, f"no 429 in {len(codes)} requests from one login"
    assert len(codes) > capacity // 2, "throttled long before the bucket's size"
