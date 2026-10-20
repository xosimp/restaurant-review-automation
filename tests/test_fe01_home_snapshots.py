"""Home's slow blocks as kept read models (scale audit 10/19/26, FE-01).

/morning-brief, /good-news, /monthly-review and /cross-module were full
builds on every Home open (13.4 s, 11.1 s, 5.8 s and 3.3 s averages in
production). home_snapshots keeps each per (restaurant, block, scope) for the
business day: a second open reads the row; a stale row is served and rebuilt
off the request; a new day, or a new login, builds.
"""
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import home_snapshots
import models
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    import metrics, outcomes, goals, menu_intelligence, demand, loss_detection, issues
    import invoices, strategy_jobs, review_intelligence, food_cost_intelligence
    import business_intelligence, morning_brief, milestones, good_news
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for m in (models, auth, metrics, outcomes, goals, menu_intelligence, demand, loss_detection,
              issues, invoices, strategy_jobs, review_intelligence, food_cost_intelligence,
              business_intelligence, morning_brief, milestones, good_news):
        monkeypatch.setattr(m, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setenv("HOME_SNAPSHOTS", "1")


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Snap Co", owner_email="s@x.com", module_reviews=1, **kw),
                             db_path=db_path)


class _Counter:
    def __init__(self, value):
        self.calls, self.value = 0, value

    def __call__(self):
        self.calls += 1
        return dict(self.value, n=self.calls)


def test_a_second_read_is_the_kept_row_not_a_build(db_path):
    rid = _rid(db_path)
    build = _Counter({"lines": ["a"]})
    today = date(2026, 10, 19)
    first = home_snapshots.serve("morning_brief", rid, "v:1", today, build)
    second = home_snapshots.serve("morning_brief", rid, "v:1", today, build)
    assert build.calls == 1
    assert first == second == {"lines": ["a"], "n": 1}


def test_a_new_business_day_or_another_scope_builds(db_path):
    rid = _rid(db_path)
    build = _Counter({"x": 1})
    today = date(2026, 10, 19)
    home_snapshots.serve("good_news", rid, "d:-", today, build)
    home_snapshots.serve("good_news", rid, "d:labor", today, build)
    assert build.calls == 2                      # a manager never reads the owner's row
    home_snapshots.serve("good_news", rid, "d:-", today + timedelta(days=1), build)
    assert build.calls == 3                      # yesterday's answer is never today's


def test_a_moved_input_serves_the_kept_row_and_rebuilds_off_the_request(db_path, monkeypatch):
    rid = _rid(db_path)
    started = []
    monkeypatch.setattr(home_snapshots, "_start_refresh", lambda key, *a, **k: started.append(key) or True)
    build = _Counter({"x": 1})
    today = date(2026, 10, 19)
    home_snapshots.serve("cross_module", rid, "v:1", today, build)
    assert not started
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ops_issues (restaurant_id, kind, title) VALUES (?,?,?)", (rid, "other", "Walk-in"))
    conn.commit()
    conn.close()
    again = home_snapshots.serve("cross_module", rid, "v:1", today, build)
    assert again["n"] == 1 and build.calls == 1  # the request did not build
    assert len(started) == 1                     # the pool did


def test_an_old_row_is_refreshed_even_when_no_stamp_moved(db_path, monkeypatch):
    rid = _rid(db_path)
    started = []
    monkeypatch.setattr(home_snapshots, "_start_refresh", lambda key, *a, **k: started.append(key) or True)
    build = _Counter({"x": 1})
    today = date(2026, 10, 19)
    home_snapshots.serve("monthly_review", rid, "all", today, build)
    conn = get_conn(db_path)
    conn.execute("UPDATE home_snapshots SET built_at = built_at - ?", (home_snapshots.REFRESH_SECONDS + 5,))
    conn.commit()
    conn.close()
    home_snapshots.serve("monthly_review", rid, "all", today, build)
    assert build.calls == 1 and len(started) == 1


def test_the_background_rebuild_writes_the_new_answer(db_path):
    rid = _rid(db_path)
    build = _Counter({"x": 1})
    today = date(2026, 10, 19)
    home_snapshots.serve("good_news", rid, "d:-", today, build)
    key = (rid, "good_news", "d:-", None)
    home_snapshots._refreshing.add(key)
    home_snapshots._refresh(key, rid, "good_news", "d:-", str(today), "s2", build, None)
    assert key not in home_snapshots._refreshing
    assert home_snapshots.serve("good_news", rid, "d:-", today, build)["n"] == 2


def test_switched_off_every_read_builds(db_path, monkeypatch):
    monkeypatch.setenv("HOME_SNAPSHOTS", "0")
    rid = _rid(db_path)
    build = _Counter({"x": 1})
    for _ in range(3):
        home_snapshots.serve("good_news", rid, "d:-", date(2026, 10, 19), build)
    assert build.calls == 3


def test_viewer_scope_separates_logins_roles_and_view_as():
    owner = {"id": 1, "role": "client"}
    assert home_snapshots.viewer_scope(owner) == home_snapshots.viewer_scope(dict(owner))
    assert home_snapshots.viewer_scope(owner) != home_snapshots.viewer_scope({"id": 2, "role": "client"})
    assert home_snapshots.viewer_scope({"id": 3, "role": "manager"}) != \
        home_snapshots.viewer_scope({"id": 3, "role": "manager", "grants": frozenset({"loss.view"})})
    assert home_snapshots.viewer_scope(owner) != \
        home_snapshots.viewer_scope(dict(owner, acting_admin_id=9, device_type="admin-view-as"))


def test_an_activity_stamp_on_the_restaurant_does_not_miss(db_path):
    """last_active_tab moves on every tab switch (/api/log-activity); a
    setting moves the stamp."""
    rid = _rid(db_path)
    s1 = home_snapshots.data_stamp(rid, models.get_restaurant(rid, db_path=db_path))
    models.update_restaurant(rid, {"last_active_tab": "labor"}, db_path=db_path)
    assert home_snapshots.data_stamp(rid, models.get_restaurant(rid, db_path=db_path)) == s1
    models.update_restaurant(rid, {"labor_target_pct": 27.0}, db_path=db_path)
    assert home_snapshots.data_stamp(rid, models.get_restaurant(rid, db_path=db_path)) != s1


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client", uid=5):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.com"})


@pytest.mark.parametrize("path,block", [("/api/morning-brief?view=home", "morning_brief"),
                                        ("/api/good-news", "good_news"),
                                        ("/api/monthly-review", "monthly_review"),
                                        ("/api/cross-module", "cross_module")])
def test_each_route_answers_the_same_from_the_kept_row(client, db_path, monkeypatch, path, block):
    rid = _rid(db_path, module_labor=1, module_inventory=1)
    _as(monkeypatch, rid)
    first = client.get(path).get_json()
    conn = get_conn(db_path)
    kept = conn.execute("SELECT COUNT(*) FROM home_snapshots WHERE restaurant_id=? AND block=?",
                        (rid, block)).fetchone()[0]
    conn.close()
    assert kept == 1

    import business_intelligence, good_news, monthly_review, morning_brief
    def _no_build(*a, **k):
        raise AssertionError("built on a kept read")
    for mod, fn in ((morning_brief, "build"), (good_news, "all_good_news"),
                    (monthly_review, "build"), (business_intelligence, "executive_brief")):
        monkeypatch.setattr(mod, fn, _no_build)
    second = client.get(path).get_json()
    assert second == first


def test_a_line_answered_after_the_build_is_not_served(client, db_path, monkeypatch):
    import morning_brief
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    monkeypatch.setattr(morning_brief, "build", lambda *a, **k: {
        "lines": [{"key": "stock", "rec": "stock:flour", "tone": "action", "text": "Order flour"},
                  {"key": "reviews", "tone": "neutral", "text": "All answered"}]})
    assert len(client.get("/api/morning-brief").get_json()["brief"]["lines"]) == 2
    import rec_ledger
    monkeypatch.setattr(rec_ledger, "silenced_keys", lambda *a, **k: {"stock:flour"})
    lines = client.get("/api/morning-brief").get_json()["brief"]["lines"]
    assert [l["key"] for l in lines] == ["reviews"]


def test_two_logins_never_share_a_brief(client, db_path, monkeypatch):
    import morning_brief
    rid = _rid(db_path)
    monkeypatch.setattr(morning_brief, "build", lambda *a, viewer=None, **k: {
        "lines": [{"key": "who", "tone": "neutral", "text": f"for {viewer['id']}"}]})
    _as(monkeypatch, rid, uid=5)
    assert client.get("/api/morning-brief").get_json()["brief"]["lines"][0]["text"] == "for 5"
    _as(monkeypatch, rid, role="manager", uid=6)
    assert client.get("/api/morning-brief").get_json()["brief"]["lines"][0]["text"] == "for 6"


def test_the_morning_send_keeps_each_recipients_home_brief(db_path, monkeypatch, client):
    """The precompute: deliver() builds every recipient's brief before they
    open Home; their first open reads it instead of building it again."""
    import morning_brief
    rid = _rid(db_path)
    auth.init_auth(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO users (id, restaurant_id, username, email, password_hash, role) "
                 "VALUES (5, ?, 'o', 'o@x.com', 'x', 'owner')", (rid,))
    conn.commit()
    conn.close()
    calls = []
    monkeypatch.setattr(morning_brief, "build", lambda *a, viewer=None, **k: calls.append(viewer["id"]) or {
        "lines": [{"key": "all_clear", "tone": "good", "text": "All clear"}]})
    monkeypatch.setattr(morning_brief, "_email_brief", lambda *a, **k: type("R", (), {"ok": True})())
    morning_brief.deliver(rid, db_path=db_path)
    assert calls == [5]
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 5, "restaurant_id": rid, "is_admin": 0, "role": "owner",
                                 "grants": frozenset(), "username": "o", "email": "o@x.com"})
    body = client.get("/api/morning-brief?view=home").get_json()
    assert body["brief"]["lines"][0]["text"] == "All clear"
    assert calls == [5]                          # read, not built


# ── one read for Home's follow-through ──────────────────────────────────────

def test_home_follow_answers_each_block_as_its_own_read_did(client, db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1, module_inventory=1)
    _as(monkeypatch, rid)
    import strategy_routes
    # Build the kept blocks first (the individual reads), then the combined
    # read must answer every block exactly as its own route does.
    own = {}
    for key, path, args, _body in strategy_routes._HOME_FOLLOW:
        qs = "&".join(f"{k}={v}" for k, v in args.items())
        r = client.get(path + ("?" + qs if qs else ""))
        d = r.get_json() if r.status_code == 200 else None
        own[key] = d if d and d.get("ok") else None
    body = client.get("/api/home/follow").get_json()
    assert body["ok"] and body["missing"] == []
    assert set(body["blocks"]) == set(own)
    for key in own:
        assert body["blocks"][key] == own[key], key


def test_home_follow_never_builds_a_kept_block_on_the_request(client, db_path, monkeypatch):
    import morning_brief, good_news, monthly_review, business_intelligence
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    def _no_build(*a, **k):
        raise AssertionError("built inside the combined read")
    for mod, fn in ((morning_brief, "build"), (good_news, "all_good_news"),
                    (monthly_review, "build"), (business_intelligence, "executive_brief")):
        monkeypatch.setattr(mod, fn, _no_build)
    body = client.get("/api/home/follow").get_json()
    assert sorted(body["missing"]) == ["brief", "cross", "month", "news"]
    assert body["blocks"]["goals"] is not None


def test_home_follow_keeps_a_blocks_own_refusal(client, db_path, monkeypatch):
    """Comps and voids are owner-only (LOSS_VIEW): a manager's combined read
    carries null for them, as their own read answered 403."""
    rid = _rid(db_path)
    _as(monkeypatch, rid, role="manager")
    assert client.get("/api/loss-signals").status_code == 403
    assert client.get("/api/home/follow").get_json()["blocks"]["loss"] is None


def _dashboard():
    import os
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_web_home_makes_one_follow_read_plus_the_report_row():
    src = _dashboard()
    body = src.split("function hbFollow(){", 1)[1].split("\n  }\n", 1)[0]
    assert "fetch('/api/home/follow'" in body
    assert "get('dsr','/api/dsr?limit=1')" in body
    # The fallback reads each block from its own route.
    assert "each();" in body and "d.missing" in body


def test_home_loads_only_when_home_is_on_screen():
    """FE-02: the page boot no longer runs Home's fan-out whatever tab, or
    Studio / Bar / DNA page, it opened on."""
    src = _dashboard()
    assert "document.addEventListener('DOMContentLoaded',function(){hbLoad(false);" not in src
    boot = src.split("function hbBoot(){", 1)[1].split("}", 1)[0]
    assert "hbOnScreen()" in boot
    on_screen = src.split("function hbOnScreen(){", 1)[1].split("\n  }", 1)[0]
    for flag in ("_SS_BOOT", "_DNA_BOOT", "_BAR_BOOT", "panel-home"):
        assert flag in on_screen
    # The pages over Home load it when they close.
    for closer in ("window.dnaClose = function", "function studioClose(opts)", "window.barClose = function"):
        assert "hbEnsure" in src.split(closer, 1)[1].split("\n};", 1)[0].split("\n  };", 1)[0], closer


def test_home_follow_restores_the_requests_own_query(client, db_path):
    import strategy_routes
    from flask import request
    rid = _rid(db_path)
    with client.application.test_request_context("/api/home/follow?x=1"):
        strategy_routes._do_home_follow({"id": 5, "restaurant_id": rid, "is_admin": 0, "role": "client"})
        assert dict(request.args) == {"x": "1"}
