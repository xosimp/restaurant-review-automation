"""The employee-auth audit's findings, each pinned so it cannot come back.

One test (or a small group) per finding, named for what it asserts rather
than for the finding id — the id is in the docstring so a future reader can
trace it back to the audit without the test names becoming a numbered list
nobody can read.

These are regression tests in the strict sense: every one of them FAILED
against the commit the audit was run at.
"""
import time

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
import staff_routes
from auth import (create_session, create_staff_session, create_user,
                  get_or_create_staff_portal_token, get_session_user, init_auth,
                  set_membership_pin, upsert_membership, verify_membership_pin)
from models import Restaurant, create_restaurant, get_conn
from staff_routes import staff_bp


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def app(db_path):
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(staff_bp)
    flask_app.register_blueprint(client_api.client_bp)
    flask_app.register_blueprint(mobile_api.mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, name="Simple EJ's", owner="o@x.test"):
    return create_restaurant(Restaurant(name=name, owner_email=owner), db_path=db_path)


def _employee(db_path, rid, username="jordan", name="Jordan P.", pin="8317",
              job_role=None):
    uid = create_user(rid, username, f"{username}@x.test", "unused", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name,
                          job_role=job_role, db_path=db_path)
    if pin:
        set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


def _console(db_path, rid, username="erik", role="client"):
    uid = create_user(rid, username, f"{username}@x.test", "pw", db_path=db_path)
    upsert_membership(uid, rid, role, db_path=db_path)
    return uid


def _login(client, token, membership_id, pin):
    roster = client.get(f"/staff/api/roster/{token}").get_json() or {}
    return client.post(f"/staff/r/{token}/login",
                       json={"membership_id": membership_id, "pin": pin,
                             "nonce": roster.get("login_nonce", "")})


# ── F-01 · permission granularity below DASHBOARD_ACCESS ──────────────────

def test_a_manager_may_read_labor_but_not_food_cost():
    """F-01. DASHBOARD_ACCESS was a single boolean: hold it and every
    non-employee route in the app was open. An owner could not hire a shift
    manager without also handing over margins."""
    from permissions import (FOOD_COST_VIEW, LABOR_VIEW, REVIEWS_VIEW,
                             has_permission)
    manager = {"role": "manager"}
    assert has_permission(manager, LABOR_VIEW)
    assert has_permission(manager, REVIEWS_VIEW)
    assert not has_permission(manager, FOOD_COST_VIEW)


def test_the_owner_and_primary_login_keep_every_module():
    from permissions import MODULE_VIEW_PERMISSIONS, has_permission
    for role in ("owner", "client"):
        for perm in MODULE_VIEW_PERMISSIONS.values():
            assert has_permission({"role": role}, perm), f"{role} lost {perm}"


def test_a_legacy_teammate_keeps_the_modules_it_has_always_had():
    """Narrowing `member` silently would take access away from live logins
    mid-shift. The new `manager` role is where a narrower teammate lives."""
    from permissions import MODULE_VIEW_PERMISSIONS, has_permission
    for perm in MODULE_VIEW_PERMISSIONS.values():
        assert has_permission({"role": "member"}, perm)


def test_an_employee_holds_no_module_permission():
    from permissions import MODULE_VIEW_PERMISSIONS, has_permission
    for perm in MODULE_VIEW_PERMISSIONS.values():
        assert not has_permission({"role": "employee"}, perm)


def test_every_module_prefix_maps_to_a_permission():
    """The path table and the permission table have to stay in step, or a
    module gains routes nothing authorises."""
    from auth import _MODULE_PREFIXES
    from permissions import MODULE_VIEW_PERMISSIONS
    keys = {key for _prefix, key in _MODULE_PREFIXES}
    assert keys == set(MODULE_VIEW_PERMISSIONS), keys ^ set(MODULE_VIEW_PERMISSIONS)


def test_a_manager_is_refused_the_food_cost_endpoint(client, db_path, monkeypatch):
    """The registry is only worth as much as its enforcement. Driven through
    the real decorator, not the permission function."""
    monkeypatch.setattr(models, "restaurant_has_module", lambda *a, **k: True)
    rid = _restaurant(db_path)
    uid = _console(db_path, rid, "mgr", role="manager")
    client.set_cookie("session_token", create_session(uid, db_path=db_path))
    resp = client.get("/api/food-cost/menu-profitability")
    assert resp.status_code == 403
    assert resp.get_json().get("module_forbidden") is True


def test_a_manager_may_still_reach_labor(client, db_path, monkeypatch):
    monkeypatch.setattr(models, "restaurant_has_module", lambda *a, **k: True)
    rid = _restaurant(db_path)
    uid = _console(db_path, rid, "mgr", role="manager")
    client.set_cookie("session_token", create_session(uid, db_path=db_path))
    assert client.get("/api/labor/team").status_code != 403


def test_a_member_is_refused_the_manager_dm_inbox(client, db_path):
    """F-01's amber cell. The DM inbox is new, so withholding it from the
    legacy teammate tier takes nothing away from anyone."""
    rid = _restaurant(db_path)
    uid = _console(db_path, rid, "teammate", role="member")
    client.set_cookie("session_token", create_session(uid, db_path=db_path))
    assert client.get("/api/team/inbox").status_code == 403


def test_an_owner_reaches_the_manager_dm_inbox(client, db_path):
    rid = _restaurant(db_path)
    uid = _console(db_path, rid, "erik", role="client")
    client.set_cookie("session_token", create_session(uid, db_path=db_path))
    assert client.get("/api/team/inbox").status_code == 200


# ── F-02 / F-13 · indexes ─────────────────────────────────────────────────

def test_sessions_is_indexed_on_user_id(db_path):
    """F-02. Every sign-in runs two DELETE … WHERE user_id=? statements; the
    only index was the token primary key, so both were full scans."""
    conn = get_conn(db_path)
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sessions'")}
        plan = conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM sessions WHERE user_id=1 "
            "AND expires_at <= datetime('now')").fetchall()
    finally:
        conn.close()
    assert "idx_sessions_user" in names
    assert any("idx_sessions_user" in str(tuple(r)) for r in plan), [tuple(r) for r in plan]


def test_login_history_is_indexed_by_user_and_time(db_path):
    """F-13."""
    conn = get_conn(db_path)
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='login_history'")}
    finally:
        conn.close()
    assert "idx_login_history_user" in names


def test_login_history_is_pruned_to_the_retention_window(db_path):
    """F-13. Append-only was never meant to mean unbounded — a staff tier
    writes a row per employee per shift, forever."""
    from auth import LOGIN_HISTORY_RETENTION_DAYS, prune_login_history
    rid = _restaurant(db_path)
    uid = _console(db_path, rid)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO login_history (user_id, restaurant_id, event, created_at) "
            "VALUES (?,?,'login', datetime('now', ?))",
            (uid, rid, f"-{LOGIN_HISTORY_RETENTION_DAYS + 5} days"))
        conn.execute(
            "INSERT INTO login_history (user_id, restaurant_id, event, created_at) "
            "VALUES (?,?,'login', datetime('now', '-2 days'))", (uid, rid))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM login_history").fetchone()["n"] == 2
    finally:
        conn.close()

    removed = prune_login_history(db_path=db_path)
    assert removed == 1
    conn = get_conn(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM login_history").fetchone()["n"] == 1
    finally:
        conn.close()


# ── F-03 · the staff cookie's Secure flag ─────────────────────────────────

def test_the_secure_flag_comes_from_the_deployment_not_a_request_header(monkeypatch):
    """F-03. It read X-Forwarded-Proto — a header the app never validated,
    normalised, or installed ProxyFix to trust."""
    from auth import cookies_require_secure
    for var in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "CAVNAR_FORCE_SECURE_COOKIES"):
        monkeypatch.delenv(var, raising=False)
    assert cookies_require_secure() is False
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert cookies_require_secure() is True


def test_a_spoofed_forwarded_proto_header_cannot_set_the_staff_cookie_secure(client, db_path, monkeypatch):
    """The exact attack the old code allowed in reverse: the header decided a
    security property, so whoever controlled it decided that property."""
    for var in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "CAVNAR_FORCE_SECURE_COOKIES"):
        monkeypatch.delenv(var, raising=False)
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    roster = client.get(f"/staff/api/roster/{token}").get_json()
    resp = client.post(f"/staff/r/{token}/login",
                       json={"membership_id": mid, "pin": "8317",
                             "nonce": roster["login_nonce"]},
                       headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200
    cookie = "".join(resp.headers.getlist("Set-Cookie"))
    assert "staff_session" in cookie
    assert "Secure" not in cookie


def test_the_app_trusts_exactly_one_proxy_hop():
    """F-03's other half. Without ProxyFix, remote_addr is the proxy for
    every visitor — which quietly merges the whole internet into one bucket
    for the per-IP portal throttle that F-10 and F-11 just fixed."""
    from auth import install_proxy_fix
    probe = Flask(__name__)

    @probe.route("/whoami")
    def whoami():
        from flask import jsonify, request as rq
        return jsonify(ip=rq.remote_addr, secure=rq.is_secure)

    plain = probe.test_client().get(
        "/whoami", headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
    assert plain.get_json()["ip"] != "203.0.113.7"

    install_proxy_fix(probe)
    fixed = probe.test_client().get(
        "/whoami", headers={"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"})
    assert fixed.get_json()["ip"] == "203.0.113.7"
    assert fixed.get_json()["secure"] is True


def test_only_the_nearest_hop_is_trusted():
    """x_for=1. A client that prepends its own X-Forwarded-For must not be
    able to choose which IP the throttle counts against."""
    from auth import install_proxy_fix
    probe = Flask(__name__)

    @probe.route("/whoami")
    def whoami():
        from flask import jsonify, request as rq
        return jsonify(ip=rq.remote_addr)

    install_proxy_fix(probe)
    spoofed = probe.test_client().get(
        "/whoami", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.7"})
    assert spoofed.get_json()["ip"] == "203.0.113.7"


# ── F-04 · one query per request, not two ─────────────────────────────────

def test_resolving_a_session_costs_a_single_query(db_path, monkeypatch):
    """F-04. The membership dual-read ran on all ~390 decorated routes as a
    second round trip."""
    rid = _restaurant(db_path)
    uid = _console(db_path, rid)
    token = create_session(uid, db_path=db_path)

    calls = []
    real = models.get_conn

    def counting(*a, **k):
        calls.append(1)
        return real(db_path)

    monkeypatch.setattr(auth, "get_conn", counting)
    user = get_session_user(token, db_path=db_path)
    assert user is not None
    assert user["membership_id"] is not None
    assert len(calls) == 1, f"{len(calls)} connections opened to resolve one session"


def test_the_session_still_resolves_the_acting_restaurants_role(db_path):
    """The join has to land on the same restaurant the overlay resolves to,
    or an owner switched into location B would read location A's role."""
    rid_a = _restaurant(db_path, "A")
    rid_b = _restaurant(db_path, "B", owner="o@x.test")
    uid = create_user(rid_a, "owner1", "owner1@x.test", "pw", db_path=db_path)
    auth.set_user_role(uid, "owner", db_path=db_path)
    upsert_membership(uid, rid_a, "owner", db_path=db_path)
    upsert_membership(uid, rid_b, "manager", db_path=db_path)

    token = create_session(uid, db_path=db_path)
    assert get_session_user(token, db_path=db_path)["role"] == "owner"
    auth.switch_active_restaurant(token, rid_b, db_path=db_path)
    switched = get_session_user(token, db_path=db_path)
    assert switched["restaurant_id"] == rid_b
    assert switched["role"] == "manager"


def test_internal_join_columns_never_leak_into_current_user(db_path):
    rid = _restaurant(db_path)
    uid = _console(db_path, rid)
    user = get_session_user(create_session(uid, db_path=db_path), db_path=db_path)
    assert not [k for k in user if k.startswith("_m_")]


# ── F-05 · the timing oracle ──────────────────────────────────────────────

def _median_ms(fn, iterations=7):
    import statistics
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


def test_a_missing_membership_costs_the_same_as_a_wrong_pin(db_path):
    """F-05. Measured at 56.66 ms vs 0.35 ms — a 160x tell that answered
    exactly the question the identical error messages refuse to."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    auth._burn_pin_cycles("0000")  # build the decoy once, outside the timing

    real = _median_ms(lambda: verify_membership_pin(mid, rid, "0000", db_path=db_path))
    missing = _median_ms(lambda: verify_membership_pin(999999, rid, "0000", db_path=db_path))
    assert missing > real * 0.4, f"miss {missing:.2f}ms vs real {real:.2f}ms"


def test_a_membership_with_no_pin_costs_the_same_as_a_wrong_pin(db_path):
    """The other early return: 'exists but no PIN set' also skipped the KDF."""
    rid = _restaurant(db_path)
    _uid, with_pin = _employee(db_path, rid, username="a", name="A", pin="8317")
    _uid2, no_pin = _employee(db_path, rid, username="b", name="B", pin=None)
    auth._burn_pin_cycles("0000")

    real = _median_ms(lambda: verify_membership_pin(with_pin, rid, "0000", db_path=db_path))
    blank = _median_ms(lambda: verify_membership_pin(no_pin, rid, "0000", db_path=db_path), 3)
    assert blank > real * 0.4, f"no-pin {blank:.2f}ms vs real {real:.2f}ms"


# ── F-06 / F-19 · the job role ────────────────────────────────────────────

def test_the_job_role_comes_from_the_membership_when_set(db_path):
    """F-06 / #15. Stored on the membership, so it is a fact an owner stated
    rather than something re-derived from shift data on every request."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.", job_role="Bartender")
    membership = auth.get_membership_by_id(mid, rid, db_path=db_path)
    assert staff_routes._employee_job_role(rid, membership) == "Bartender"


def test_the_job_role_is_never_derived_from_sample_data(db_path, monkeypatch):
    """F-06. It called labor.load_shifts_for_restaurant, whose own docstring
    says it returns bundled SAMPLE shifts when no CSV exists — and the job
    role decides which checklist an employee may complete."""
    import labor
    called = []
    real_loader = labor.load_shifts_for_restaurant

    def tripwire(*a, **k):
        called.append(1)
        return real_loader(*a, **k)

    monkeypatch.setattr(labor, "load_shifts_for_restaurant", tripwire)
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.")
    membership = auth.get_membership_by_id(mid, rid, db_path=db_path)
    assert staff_routes._employee_job_role(rid, membership) is None
    assert not called, "the sample-capable loader was reached"


def test_the_job_role_is_resolved_once_per_request(app, db_path):
    """F-19. Both task endpoints call it and step 3 parses a whole schedule
    CSV, so uncached it was O(all shifts) per tap of a checkbox."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.")
    membership = auth.get_membership_by_id(mid, rid, db_path=db_path)
    resolved = []
    original = staff_routes._resolve_job_role_from_data

    def counting(restaurant_id, name):
        resolved.append(name)
        return original(restaurant_id, name)

    staff_routes._resolve_job_role_from_data = counting
    try:
        with app.test_request_context("/staff/api/tasks"):
            staff_routes._employee_job_role(rid, membership)
            staff_routes._employee_job_role(rid, membership)
            staff_routes._employee_job_role(rid, membership)
    finally:
        staff_routes._resolve_job_role_from_data = original
    assert len(resolved) == 1, resolved


# ── F-07 · task_date ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["2099-12-31", "1970-01-01", "not-a-date",
                                 "'; DROP TABLE x;--", "", "2026-13-45"])
def test_a_task_cannot_be_filed_under_an_arbitrary_date(client, db_path, bad):
    """F-07. Not injection — queries are parameterised — but an employee
    could pre-complete a month of checklists, which defeats the only purpose
    the feature has."""
    rid = _restaurant(db_path)
    uid, _mid = _employee(db_path, rid, name="Jordan P.", job_role="Server")
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    resp = client.post("/staff/api/tasks/complete",
                       json={"template_id": 1, "task_date": bad, "done": True})
    assert resp.status_code == 400, f"{bad!r} was accepted"


def test_todays_date_is_still_accepted():
    from datetime import date, timedelta
    assert staff_routes._valid_task_date(date.today().isoformat())
    # ±1 day, so a closing task ticked after midnight still files correctly.
    assert staff_routes._valid_task_date((date.today() - timedelta(days=1)).isoformat())
    assert staff_routes._valid_task_date((date.today() + timedelta(days=1)).isoformat())
    assert staff_routes._valid_task_date((date.today() + timedelta(days=3)).isoformat()) is None


# ── F-08 · the dead session TTL ───────────────────────────────────────────

def test_a_staff_session_survives_a_quiet_shift(db_path):
    """F-08. STAFF_SESSION_HOURS was dead code: the 8-hour inactivity rule
    exempted only iOS, so an employee who signed in at the start of a double
    and did not touch the portal was signed out mid-shift."""
    rid = _restaurant(db_path)
    uid, _mid = _employee(db_path, rid)
    token = create_staff_session(uid, rid, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sessions SET last_active=datetime('now','-9 hours') "
                     "WHERE token=?", (auth.hash_session_token(token),))
        conn.commit()
    finally:
        conn.close()
    assert get_session_user(token, db_path=db_path) is not None


def test_a_web_console_session_still_times_out_on_inactivity(db_path):
    """The exemption must not have widened to everything."""
    rid = _restaurant(db_path)
    uid = _console(db_path, rid)
    token = create_session(uid, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sessions SET last_active=datetime('now','-9 hours') "
                     "WHERE token=?", (auth.hash_session_token(token),))
        conn.commit()
    finally:
        conn.close()
    assert get_session_user(token, db_path=db_path) is None


def test_a_staff_session_still_dies_at_its_hard_expiry(db_path):
    """Exempting inactivity must not make a staff session immortal — the
    shift-length hard expiry is now the only control, so it has to work."""
    rid = _restaurant(db_path)
    uid, _mid = _employee(db_path, rid)
    token = create_staff_session(uid, rid, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE sessions SET expires_at=datetime('now','-1 hour') "
                     "WHERE token=?", (auth.hash_session_token(token),))
        conn.commit()
    finally:
        conn.close()
    assert get_session_user(token, db_path=db_path) is None


# ── F-09 · the organization layer ─────────────────────────────────────────

def test_a_location_group_becomes_a_real_organization(db_path):
    """F-09. The group used to be an emergent property of two strings
    agreeing: a typo silently created a second group."""
    from models import backfill_organizations, get_location_group
    rid_a = create_restaurant(Restaurant(name="Syrup North", owner_email="Group@x.test",
                                         location_group="Syrup"), db_path=db_path)
    rid_b = create_restaurant(Restaurant(name="Syrup South", owner_email="group@x.test",
                                         location_group="Syrup"), db_path=db_path)
    linked = backfill_organizations(db_path=db_path)
    assert linked >= 2

    conn = get_conn(db_path)
    try:
        orgs = conn.execute("SELECT id FROM organizations").fetchall()
        rows = conn.execute("SELECT id, organization_id FROM restaurants "
                            "WHERE id IN (?,?)", (rid_a, rid_b)).fetchall()
    finally:
        conn.close()
    assert len(orgs) == 1, "one group, one organization"
    assert all(r["organization_id"] == orgs[0]["id"] for r in rows)

    group = get_location_group("Syrup", db_path=db_path, owner_email="group@x.test")
    assert {r["id"] for r in group} == {rid_a, rid_b}


def test_the_organization_backfill_is_idempotent(db_path):
    from models import backfill_organizations
    create_restaurant(Restaurant(name="Syrup North", owner_email="g@x.test",
                                 location_group="Syrup"), db_path=db_path)
    assert backfill_organizations(db_path=db_path) == 1
    assert backfill_organizations(db_path=db_path) == 0


def test_two_owners_sharing_a_group_name_stay_separate(db_path):
    """The collision the owner_email scope was added for, now expressed as
    two organization rows rather than a string match plus a guard."""
    from models import backfill_organizations, get_location_group
    mine = create_restaurant(Restaurant(name="Syrup A", owner_email="me@x.test",
                                        location_group="Syrup"), db_path=db_path)
    theirs = create_restaurant(Restaurant(name="Syrup B", owner_email="them@x.test",
                                          location_group="Syrup"), db_path=db_path)
    backfill_organizations(db_path=db_path)
    got = get_location_group("Syrup", db_path=db_path, owner_email="me@x.test")
    assert [r["id"] for r in got] == [mine]
    assert theirs not in [r["id"] for r in got]


def test_a_single_location_client_gets_no_organization(db_path):
    """A restaurant with no group is not a group of one."""
    from models import backfill_organizations
    create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.test"),
                      db_path=db_path)
    assert backfill_organizations(db_path=db_path) == 0


def test_writing_a_group_name_keeps_the_organization_in_step(db_path):
    from models import get_restaurant, update_restaurant
    rid = _restaurant(db_path, "Syrup North", owner="g@x.test")
    update_restaurant(rid, {"location_group": "Syrup"}, db_path=db_path)
    conn = get_conn(db_path)
    try:
        org_id = conn.execute("SELECT organization_id FROM restaurants WHERE id=?",
                              (rid,)).fetchone()["organization_id"]
    finally:
        conn.close()
    assert org_id is not None
    assert get_restaurant(rid, db_path=db_path) is not None


# ── F-10 / F-11 · the portal throttle ─────────────────────────────────────

def test_the_roster_endpoint_is_throttled(client, db_path):
    """F-10. Neither roster path called the throttle at all, so token
    guessing and roster enumeration had no ceiling. Guesses (unknown codes)
    spend the failure budget; reads with the real code only the overall
    ceiling, so a kitchen on one Wi-Fi can open the roster at shift change
    (SEC-18)."""
    from auth import PORTAL_MAX_ATTEMPTS, PORTAL_MAX_REQUESTS
    rid = _restaurant(db_path)
    _employee(db_path, rid)
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    guesses = [client.get(f"/staff/api/roster/not-{i}", environ_base={"REMOTE_ADDR": "198.51.100.1"}).status_code
               for i in range(PORTAL_MAX_ATTEMPTS + 2)]
    assert 429 in guesses, guesses[-3:]
    reads = [client.get(f"/staff/api/roster/{token}", environ_base={"REMOTE_ADDR": "198.51.100.2"}).status_code
             for _ in range(PORTAL_MAX_REQUESTS + 2)]
    assert reads[:PORTAL_MAX_ATTEMPTS + 2] == [200] * (PORTAL_MAX_ATTEMPTS + 2)
    assert 429 in reads, reads[-3:]


def test_the_html_sign_in_screen_is_throttled(client, db_path):
    from auth import PORTAL_MAX_ATTEMPTS, PORTAL_MAX_REQUESTS
    rid = _restaurant(db_path)
    _employee(db_path, rid)
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    guesses = [client.get(f"/staff/r/not-{i}", environ_base={"REMOTE_ADDR": "198.51.100.3"}).status_code
               for i in range(PORTAL_MAX_ATTEMPTS + 2)]
    assert 429 in guesses
    reads = [client.get(f"/staff/r/{token}", environ_base={"REMOTE_ADDR": "198.51.100.4"}).status_code
             for _ in range(PORTAL_MAX_REQUESTS + 2)]
    assert 429 in reads


def test_the_throttle_survives_a_restart(db_path):
    """F-11. A module dict reset on every deploy and was per-process, so the
    effective limit was 30 x worker count."""
    from auth import PORTAL_MAX_ATTEMPTS, portal_attempts_exceeded, record_portal_attempt
    for _ in range(PORTAL_MAX_ATTEMPTS):
        record_portal_attempt("203.0.113.9", db_path=db_path)
    assert portal_attempts_exceeded("203.0.113.9", db_path=db_path)
    # A different process reading the same database sees the same budget.
    conn = get_conn(db_path)
    try:
        stored = conn.execute("SELECT COUNT(*) AS n FROM portal_attempts "
                              "WHERE ip=?", ("203.0.113.9",)).fetchone()["n"]
    finally:
        conn.close()
    assert stored >= PORTAL_MAX_ATTEMPTS


def test_the_throttle_evicts_what_ages_out(db_path):
    """F-11's third problem: no key was ever deleted, so it leaked one entry
    per IP forever."""
    from auth import PORTAL_WINDOW_SECONDS, portal_attempts_exceeded, record_portal_attempt
    conn = get_conn(db_path)
    try:
        for _ in range(50):
            conn.execute("INSERT INTO portal_attempts (ip, created_at) "
                         "VALUES ('198.51.100.4', datetime('now', ?))",
                         (f"-{PORTAL_WINDOW_SECONDS + 60} seconds",))
        conn.commit()
    finally:
        conn.close()
    assert not portal_attempts_exceeded("198.51.100.4", db_path=db_path)
    record_portal_attempt("198.51.100.4", db_path=db_path)
    conn = get_conn(db_path)
    try:
        left = conn.execute("SELECT COUNT(*) AS n FROM portal_attempts").fetchone()["n"]
    finally:
        conn.close()
    assert left == 1, f"{left} stale rows survived"


# ── F-12 · day-two operations ─────────────────────────────────────────────

def test_an_owner_can_correct_a_misspelled_employee_name(client, db_path):
    """F-12. employee_name is the join key to seven name-keyed tables, so a
    typo silently detached someone from their own schedule with no way back
    short of a database console."""
    rid = _restaurant(db_path)
    owner = _console(db_path, rid)
    _uid, mid = _employee(db_path, rid, name="Jordn P.")
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    resp = client.post(f"/api/account/staff/{mid}", json={"employee_name": "Jordan P."})
    assert resp.status_code == 200
    assert auth.get_membership_by_id(mid, rid, db_path=db_path)["employee_name"] == "Jordan P."


def test_an_owner_can_promote_an_employee(client, db_path):
    rid = _restaurant(db_path)
    owner = _console(db_path, rid)
    _uid, mid = _employee(db_path, rid)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}", json={"role": "manager"}).status_code == 200
    assert auth.get_membership_by_id(mid, rid, db_path=db_path)["role"] == "manager"


def test_an_owner_can_reactivate_a_former_employee(client, db_path):
    """set_membership_active(…, True) existed and was reachable from nothing,
    so a re-hire needed database access."""
    rid = _restaurant(db_path)
    owner = _console(db_path, rid)
    _uid, mid = _employee(db_path, rid)
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}", json={"active": True}).status_code == 200
    assert auth.get_membership_by_id(mid, rid, db_path=db_path)["is_active"] == 1


def test_a_deactivated_employee_stays_visible_to_the_owner(client, db_path):
    """Invisible is unreactivatable."""
    rid = _restaurant(db_path)
    owner = _console(db_path, rid)
    _uid, mid = _employee(db_path, rid)
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    listed = client.get("/api/account/staff").get_json()
    row = [s for s in listed["staff"] if s["membership_id"] == mid]
    assert row and row[0]["active"] is False


def test_a_deactivated_employee_is_gone_from_the_sign_in_roster(client, db_path):
    """…but include_inactive is for the OWNER list only."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.")
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    roster = client.get(f"/staff/api/roster/{token}").get_json()
    assert not [r for r in roster["roster"] if r["membership_id"] == mid]


def test_an_owner_cannot_update_another_restaurants_employee(client, db_path):
    rid_a = _restaurant(db_path, "A")
    rid_b = _restaurant(db_path, "B", owner="other@x.test")
    owner_a = _console(db_path, rid_a, "erik")
    _uid, foreign = _employee(db_path, rid_b, username="stranger", name="Stranger")
    client.set_cookie("session_token", create_session(owner_a, db_path=db_path))
    resp = client.post(f"/api/account/staff/{foreign}", json={"employee_name": "Hacked"})
    assert resp.status_code == 404
    assert auth.get_membership_by_id(foreign, rid_b, db_path=db_path)["employee_name"] == "Stranger"


def test_nobody_can_promote_themselves_to_owner_through_this_route(client, db_path):
    """Owner is account-level, not restaurant-level. Nothing in this product
    should hand it out over an API."""
    rid = _restaurant(db_path)
    owner = _console(db_path, rid)
    _uid, mid = _employee(db_path, rid)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}", json={"role": "owner"}).status_code == 400
    assert auth.get_membership_by_id(mid, rid, db_path=db_path)["role"] == "employee"


def test_a_manager_cannot_manage_staff_accounts(client, db_path):
    rid = _restaurant(db_path)
    mgr = _console(db_path, rid, "mgr", role="manager")
    _uid, mid = _employee(db_path, rid)
    client.set_cookie("session_token", create_session(mgr, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}", json={"active": False}).status_code == 403


def test_a_blank_name_is_refused(db_path):
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.")
    with pytest.raises(ValueError):
        auth.update_membership_details(mid, rid, employee_name="   ", db_path=db_path)


def test_a_demotion_ends_the_staff_sessions_it_granted(db_path):
    """A permission change has to take effect now, not in fourteen hours."""
    rid = _restaurant(db_path)
    uid, mid = _employee(db_path, rid)
    token = create_staff_session(uid, rid, db_path=db_path)
    assert get_session_user(token, db_path=db_path) is not None
    auth.update_membership_details(mid, rid, role="manager", db_path=db_path)
    assert get_session_user(token, db_path=db_path) is None


# ── F-14 · replay ─────────────────────────────────────────────────────────

def test_a_captured_sign_in_cannot_be_replayed(client, db_path):
    """F-14. The body was {membership_id, pin} and nothing varied between
    requests, so a captured one worked verbatim until the PIN changed."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    roster = client.get(f"/staff/api/roster/{token}").get_json()
    body = {"membership_id": mid, "pin": "8317", "nonce": roster["login_nonce"]}

    assert client.post(f"/staff/r/{token}/login", json=body).status_code == 200
    replay = client.post(f"/staff/r/{token}/login", json=body)
    assert replay.status_code == 409
    assert replay.get_json()["nonce_expired"] is True


def test_a_nonce_from_another_restaurant_is_refused(client, db_path):
    rid_a = _restaurant(db_path, "A")
    rid_b = _restaurant(db_path, "B", owner="other@x.test")
    _uid, mid = _employee(db_path, rid_a, pin="8317")
    token_a = get_or_create_staff_portal_token(rid_a, db_path=db_path)
    token_b = get_or_create_staff_portal_token(rid_b, db_path=db_path)
    foreign = client.get(f"/staff/api/roster/{token_b}").get_json()["login_nonce"]
    resp = client.post(f"/staff/r/{token_a}/login",
                       json={"membership_id": mid, "pin": "8317", "nonce": foreign})
    assert resp.status_code == 409


def test_the_nonce_is_spent_before_the_pin_is_checked(client, db_path):
    """Otherwise a replay of a CORRECT PIN would still authenticate."""
    from auth import consume_portal_nonce, issue_portal_nonce
    rid = _restaurant(db_path)
    nonce = issue_portal_nonce(rid, db_path=db_path)
    assert consume_portal_nonce(nonce, rid, db_path=db_path) is True
    assert consume_portal_nonce(nonce, rid, db_path=db_path) is False


def test_a_wrong_pin_hands_back_a_fresh_nonce(client, db_path):
    """A mistyped PIN must not cost the employee an extra round trip."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    resp = _login(client, token, mid, "0000")
    assert resp.status_code == 401
    assert resp.get_json().get("login_nonce")


# ── F-15 · the backfill's connection ──────────────────────────────────────

def test_the_membership_backfill_runs_under_the_apps_pragmas(db_path, monkeypatch):
    """F-15. It used raw sqlite3.connect, so PRAGMA foreign_keys=ON was not
    applied during the migration — harmless for this INSERT…SELECT, and
    inconsistent with every other write path in the codebase."""
    used = []
    real = models.get_conn
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: (used.append(1), real(db_path))[1])
    rid = _restaurant(db_path)
    create_user(rid, "someone", "someone@x.test", "pw", db_path=db_path)
    conn = real(db_path)
    try:
        conn.execute("DELETE FROM memberships")
        conn.commit()
    finally:
        conn.close()
    used.clear()
    assert auth.backfill_memberships(db_path=db_path) >= 1
    assert used, "backfill bypassed get_conn"


# ── F-16 · orphaned lockout counters ──────────────────────────────────────

def test_a_deleted_membership_takes_its_lockout_counter_with_it(db_path):
    """F-16. The cascade, for databases created since it was declared."""
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid)
    verify_membership_pin(mid, rid, "0000", db_path=db_path)
    conn = get_conn(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM membership_pin_attempts "
                            "WHERE membership_id=?", (mid,)).fetchone()["n"] == 1
        conn.execute("DELETE FROM memberships WHERE id=?", (mid,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM membership_pin_attempts "
                            "WHERE membership_id=?", (mid,)).fetchone()["n"] == 0
    finally:
        conn.close()


def test_orphaned_lockout_counters_are_swept_at_boot(db_path):
    """F-16's other half. SQLite cannot add a foreign-key action to a live
    table without rebuilding it, so databases that predate the cascade need
    the sweep — this simulates one by writing an orphan with FKs off."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("INSERT INTO membership_pin_attempts (membership_id, failed_count) "
                     "VALUES (424242, 3)")
        conn.commit()
    finally:
        conn.close()
    assert auth.sweep_orphan_pin_attempts(db_path=db_path) == 1
    assert auth.sweep_orphan_pin_attempts(db_path=db_path) == 0


# ── F-17 · auditable sign-in kinds ────────────────────────────────────────

def test_a_staff_sign_in_is_distinguishable_in_the_history(db_path):
    """F-17. 'Who opened the portal and when' has to be answerable from the
    audit table itself, not by joining to a nullable column on sessions."""
    rid = _restaurant(db_path)
    uid, _mid = _employee(db_path, rid)
    console_uid = _console(db_path, rid, "erik")
    create_staff_session(uid, rid, db_path=db_path)
    create_session(console_uid, restaurant_id=rid, db_path=db_path)

    conn = get_conn(db_path)
    try:
        rows = {r["user_id"]: r["event"] for r in conn.execute(
            "SELECT user_id, event FROM login_history").fetchall()}
    finally:
        conn.close()
    assert rows[uid] == "staff_login"
    assert rows[console_uid] == "login"


# ── F-18 · PIN failures as security events ────────────────────────────────

def test_a_failed_pin_is_recorded_as_a_security_event(db_path):
    """F-18. membership_pin_attempts is a COUNTER: it resets to zero when the
    lockout fires and is deleted on the next success, so one failed attempt
    against each of forty employees left no trace anywhere."""
    from auth import get_pin_security_events
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.", pin="8317")
    verify_membership_pin(mid, rid, "0000", ip_address="203.0.113.7", db_path=db_path)
    events = get_pin_security_events(rid, db_path=db_path)
    assert len(events) == 1
    assert events[0]["event"] == "pin_failed"
    assert events[0]["name"] == "Jordan P."
    assert events[0]["ip_address"] == "203.0.113.7"


def test_a_lockout_is_recorded_even_though_the_counter_resets(db_path):
    from auth import PIN_MAX_ATTEMPTS, get_pin_security_events
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    for _ in range(PIN_MAX_ATTEMPTS):
        verify_membership_pin(mid, rid, "0000", db_path=db_path)
    events = get_pin_security_events(rid, db_path=db_path)
    assert any(e["event"] == "pin_locked" for e in events)
    # The counter was zeroed by the lockout; the record was not.
    assert auth.pin_lockout_state(mid, db_path=db_path)["failed_count"] == 0


def test_a_successful_sign_in_does_not_erase_the_failure_record(db_path):
    """The specific reason the counter could not answer the question."""
    from auth import get_pin_security_events
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"]
    assert len(get_pin_security_events(rid, db_path=db_path)) == 1


def test_pin_events_do_not_cross_tenants(db_path):
    from auth import get_pin_security_events
    rid_a = _restaurant(db_path, "A")
    rid_b = _restaurant(db_path, "B", owner="other@x.test")
    _uid, mid = _employee(db_path, rid_a, pin="8317")
    verify_membership_pin(mid, rid_a, "0000", db_path=db_path)
    assert get_pin_security_events(rid_b, db_path=db_path) == []


# ── Audit item #23 · owners can see portal access ─────────────────────────

def test_an_owner_can_be_told_when_someone_opens_the_staff_portal(client, db_path, monkeypatch):
    """The console has had sign-in notifications since there was one account
    to notify about. The portal added a much larger set of sign-ins, on
    shared devices, and none of them were visible to the owner at all."""
    import notify
    fired = []
    monkeypatch.setattr(notify, "send_staff_signin_alert",
                        lambda *a, **k: fired.append(a))
    rid = _restaurant(db_path)
    models.update_restaurant(rid, {"staff_signin_notify": 1}, db_path=db_path)
    _uid, mid = _employee(db_path, rid, name="Jordan P.", pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    assert _login(client, token, mid, "8317").status_code == 200
    assert fired, "no staff sign-in alert fired"
    assert "Jordan P." in fired[0]


def test_the_staff_signin_alert_is_off_by_default(client, db_path, monkeypatch):
    """The portal is meant to be used every shift — a default alarm becomes
    noise and then gets switched off entirely."""
    import notify
    fired = []
    monkeypatch.setattr(notify, "send_staff_signin_alert",
                        lambda *a, **k: fired.append(a))
    rid = _restaurant(db_path)
    _uid, mid = _employee(db_path, rid, pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    assert _login(client, token, mid, "8317").status_code == 200
    assert not fired


# ── Audit item #15 · the job role is stated, not inferred ─────────────────

def test_a_new_staff_account_records_the_job_an_owner_typed(client, db_path):
    rid = _restaurant(db_path)
    owner = _console(db_path, rid)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    made = client.post("/api/account/staff",
                       json={"employee_name": "Jordan P.", "job_role": "Bartender",
                             "pin": "8317"}).get_json()
    assert made["ok"] is True
    assert made["job_role"] == "Bartender"
    stored = auth.get_membership_by_id(made["membership_id"], rid, db_path=db_path)
    assert stored["job_role"] == "Bartender"
