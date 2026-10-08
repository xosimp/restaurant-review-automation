"""Parity-round follow-ups (10/7/26): the admin platform alert, the coverage
push category, the drafted push's job id, the shift-request permission, the
lineup brief's push-only notice, the web's labor places and Marketing poll,
and the bell's approve holding the reply the row showed."""
import os
import re
import sqlite3
from datetime import datetime, timezone

import pytest

import auth
import client_api
import models
import nav
import ops
import push
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import strategy_jobs
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (push, strategy_jobs, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


def _rid(db_path, **kw):
    fields = dict(name="Gia Mia", owner_email="o@x.test", owner_name="Sam Owner", module_reviews=1,
                  module_labor=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _exec(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _device(db_path, rid, uid, tok):
    _exec(db_path, "INSERT INTO device_tokens (restaurant_id, user_id, apns_token) VALUES (?,?,?)",
          (rid, uid, tok * 20))


class _Recorder:
    def __init__(self):
        self.rows = []

    def submit(self, fn, *a):
        self.rows.append(a)


# ── 1. the platform alert: its own sheet, admins only ──────────────────────

def test_the_platform_alert_opens_the_admin_sheet_with_its_lines():
    when = datetime(2026, 10, 7, 14, 5, tzinfo=timezone.utc)
    lines = ["The scheduler has not ticked for 12 minutes.", "", "x" * 500] + [f"line {i}" for i in range(9)]
    d = ops.platform_alert_payload("Cavnar AI: scheduler stopped", lines, now=when)
    assert d["nav"] == "admin/platform"
    assert d["subject"] == "Cavnar AI: scheduler stopped"
    assert d["alert_at"] == "2026-10-07T14:05:00Z"
    # Blank lines dropped, each clipped, at most six.
    assert len(d["lines"]) == ops.PLATFORM_ALERT_PUSH_LINES
    assert d["lines"][0].startswith("The scheduler")
    assert len(d["lines"][1]) == ops.PLATFORM_ALERT_PUSH_LINE_CHARS
    # Its bell place and its push agree; old apps still degrade to Home.
    assert nav.for_notification("platform_alert") == "admin/platform"
    assert push.nav_for("platform_alert", {}) == "admin/platform"
    assert push.module_of("platform_alert") == "home"


def test_alert_will_pushes_the_sheet_to_the_admin_logins(db_path, monkeypatch):
    import emails

    rid = _rid(db_path)
    admin = auth.create_user(rid, "will", "w@x.test", "pw-Very-Long-123!", is_admin=True, db_path=db_path)
    auth.create_user(rid, "owner1", "o@x.test", "pw-Very-Long-123!", db_path=db_path, role="owner")
    monkeypatch.setattr(ops, "will_phone", lambda: None)

    class _Ok:
        ok = True
    monkeypatch.setattr(emails, "_send_branded", lambda *a, **k: _Ok())
    got = []
    monkeypatch.setattr(push, "fire_push", lambda r, t, title, body, data=None, user_ids=None, **k:
                        got.append((r, t, title, data, set(user_ids))) or 1)
    res = ops.alert_will("Scheduler stopped", ["No tick for 12 minutes.", "Jobs are overdue."])
    assert res.channels["push"] is True
    assert len(got) == 1
    r, t, title, data, ids = got[0]
    assert (r, t, ids) == (rid, "platform_alert", {admin})
    assert data["nav"] == "admin/platform" and "tab" not in data
    assert data["lines"] == ["No tick for 12 minutes.", "Jobs are overdue."]
    assert data["subject"] == "Scheduler stopped" and data["alert_at"].endswith("Z")


def test_only_admin_phones_ever_receive_a_platform_alert(db_path, monkeypatch):
    rid = _rid(db_path)
    admin = auth.create_user(rid, "will", "w@x.test", "pw-Very-Long-123!", is_admin=True, db_path=db_path)
    owner = auth.create_user(rid, "owner1", "o@x.test", "pw-Very-Long-123!", db_path=db_path, role="owner")
    _device(db_path, rid, admin, "a")
    _device(db_path, rid, owner, "b")
    rec = _Recorder()
    monkeypatch.setattr(push, "_push_executor", lambda: rec)
    # Even a caller naming the owner's login (or nobody) reaches the admin only.
    assert push.fire_push(rid, "platform_alert", "Down", "x", data={}, db_path=db_path,
                          user_ids={admin, owner}) == 1
    assert push.fire_push(rid, "platform_alert", "Down", "x", data={}, db_path=db_path) == 1
    assert {int(a[0]["user_id"]) for a in rec.rows} == {admin}
    # Any other type still reaches both.
    rec.rows.clear()
    assert push.fire_push(rid, "labor_over", "Over", "x", data={}, db_path=db_path) == 2


# ── 2. Ask someone to cover only on a shift to cover ───────────────────────

@pytest.mark.parametrize("alert_type,data,category", [
    ("coverage", {"issue_id": 4}, push.CATEGORY_COVERAGE),
    ("issue", {"issue_id": 4, "issue_kind": "no_show"}, push.CATEGORY_COVERAGE),
    ("issue", {"issue_id": 4, "issue_kind": "coverage"}, push.CATEGORY_COVERAGE),
    ("issue_escalated", {"issue_id": 4, "issue_kind": "Coverage"}, push.CATEGORY_COVERAGE),
    ("issue", {"issue_id": 4, "issue_kind": "stock"}, push.CATEGORY_ISSUE),
    ("issue", {"issue_id": 4}, push.CATEGORY_ISSUE),
    ("issue_escalated", {"issue_id": 4}, push.CATEGORY_ISSUE),
    # Nothing to post ask-cover against: Resolved's category (it opens).
    ("coverage", {}, push.CATEGORY_ISSUE),
    # A recommendation riding a coverage issue keeps the cover buttons.
    ("coverage", {"issue_id": 4, "rec_key": "k", "answerable": True}, push.CATEGORY_COVERAGE),
    # critical_low keeps Draft order, whatever it carries.
    ("critical_low", {"issue_id": 4, "issue_kind": "coverage"}, push.CATEGORY_STOCK),
])
def test_the_cover_button_is_only_on_a_shift_to_cover(alert_type, data, category):
    assert push._category(alert_type, data) == category


def test_the_app_registers_the_coverage_category_with_both_buttons():
    src = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Push/PushManager.swift"), encoding="utf-8").read()
    assert f'"{push.CATEGORY_COVERAGE}"' in src and f'"{push.CATEGORY_ISSUE}"' in src
    assert "coverageActions" in src


def test_an_issue_push_names_its_kind(db_path, monkeypatch):
    import issues
    import notify
    rid = _rid(db_path)
    issue, _tok = issues.create_issue(rid, "no_show", "Dana didn't clock in", notify=False, db_path=db_path)
    _exec(db_path, "UPDATE ops_issues SET notify_suppressed=0 WHERE id=?", (issue["id"],))
    monkeypatch.setattr(notify, "alert_audience", lambda *a, **k: None)
    got = []
    monkeypatch.setattr(push, "fire_push", lambda r, t, title, body, data=None, **k: got.append((t, data)) or 1)
    issues._push_instead(issue["id"], db_path=db_path)
    assert got == [("issue", {"issue_id": issue["id"], "surface": "issue", "issue_kind": "no_show"})]
    assert push._category(*got[0]) == push.CATEGORY_COVERAGE


# ── 4. the shift-request permission the phone gates on ─────────────────────

def test_the_shift_request_list_says_whether_this_login_may_decide(db_path, monkeypatch):
    import strategy_routes
    import shift_requests
    monkeypatch.setattr(shift_requests, "for_manager", lambda rid: [])
    monkeypatch.setattr(shift_requests, "open_shifts", lambda rid: [])
    monkeypatch.setattr(shift_requests, "live_offers", lambda rid: [])
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: u.get("draft") is True)
    ok, _ = strategy_routes._do_shift_requests_list({"restaurant_id": 1, "draft": True})
    no, _ = strategy_routes._do_shift_requests_list({"restaurant_id": 1, "draft": False})
    assert ok["can_decide"] is True and no["can_decide"] is False
    # The same helper every write route under it holds.
    src = open(os.path.join(ROOT, "strategy_routes.py"), encoding="utf-8").read()
    for fn in ("_do_shift_request_decide", "_do_open_shift_post", "_do_shift_request_offer",
               "_do_shift_request_colleague_agreed", "_do_shift_request_cancel"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 1)]
        assert "if not _may_draft(u):" in body, fn


# ── 5. tonight's lineup brief: a push, never an email ──────────────────────

def test_the_lineup_brief_waiting_notice_is_never_emailed(db_path, monkeypatch):
    import emails
    import morning_brief
    import notify
    import staff_brief
    rid = _rid(db_path)
    phone = auth.create_user(rid, "gm", "gm@x.test", "pw-Very-Long-123!", db_path=db_path, role="owner")
    nophone = auth.create_user(rid, "am", "am@x.test", "pw-Very-Long-123!", db_path=db_path, role="owner")
    _device(db_path, rid, phone, "p")
    people = [{"id": phone, "email": "gm@x.test", "role": "owner", "is_admin": 0},
              {"id": nophone, "email": "am@x.test", "role": "owner", "is_admin": 0}]
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: people)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(notify, "alert_permissions", lambda *a, **k: [])
    import permissions
    monkeypatch.setattr(permissions, "has_permission", lambda u, p: True)
    emailed, pushed = [], []
    monkeypatch.setattr(emails, "deliver", lambda **k: emailed.append(k))
    monkeypatch.setattr(push, "fire_push", lambda r, t, title, body, data=None, user_ids=None, **k:
                        pushed.append((t, set(user_ids or ()))) or 1)
    assert staff_brief.announce_waiting(rid, "2026-10-07", db_path=db_path) == 1
    assert pushed == [("lineup_brief_waiting", {phone})]
    assert emailed == []
    # _reach itself still emails by default (the other notices keep it).
    emailed.clear()
    import strategy_jobs
    strategy_jobs._reach(rid, "labor_reminder", "Waiting on you", "Two requests.", {}, db_path)
    assert [e["payload"]["to"] for e in emailed] == [["am@x.test"]]


# ── 3. the drafted push names its generation ───────────────────────────────

def test_the_drafted_push_carries_its_job_id():
    src = open(os.path.join(ROOT, "schedule_engine.py"), encoding="utf-8").read()
    fn = src[src.index("def notify_generation_watchers"):src.index("def generation_request")]
    assert '"job_id": str(job_id)' in fn


# ── 6 / 9. the web's labor places and Marketing's one poll ─────────────────

def _dash():
    return open(os.path.join(ROOT, "templates/dashboard.html"), encoding="utf-8").read()


def test_the_web_opens_labor_overtime_and_waiting_on_you():
    src = _dash()
    handler = src[src.index("cavNavRegister('labor', function (p) {"):]
    handler = handler[:handler.index("\n  });")]
    assert "p.rest[0] === 'overtime' || p.rest[0] === 'waiting'" in handler
    assert "$id('lb2-wait')" in handler and "$id('lb2-ot-lane')" in handler
    assert "{% if key == 'ot' %} id=\"lb2-ot-lane\"{% endif %}" in src
    assert 'id="lb2-wait"' in src
    # The places the bell and the push name.
    assert nav.for_notification("labor_over") == "labor/overtime"
    assert nav.for_notification("labor_reminder") == "labor/waiting"


def test_marketing_starts_one_poll_loop_per_read():
    src = _dash()
    fn = src[src.index("function loadMktInsight(){"):src.index("function renderDiagnosis(")]
    guard = fn.index("if(window._mktInsightPolling)return;")
    assert guard < fn.index("insightSwr('/api/mkt-insight'")
    assert "if(!interim)window._mktInsightPolling=false;" in fn
    assert fn.count("window._mktInsightPolling=false;") == 2


# ── 10. the bell's approve holds the reply the row showed ──────────────────

def _drafted(db_path, rid, draft):
    return _exec(db_path, """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
               review_date, fetched_at, sentiment, urgency, processed, response_status, draft_response)
               VALUES (?,'google',?,'Ann',5,'Lovely',datetime('now','-1 days'),datetime('now','-1 days'),
                       'positive','normal',1,'drafted',?)""", (rid, f"ext-{len(draft)}", draft))


def test_bell_rows_carry_whether_the_draft_is_whole_and_its_hash(db_path):
    rid = _rid(db_path)
    short = _drafted(db_path, rid, "Thanks so much, Ann!")
    long_ = _drafted(db_path, rid, "Thank you. " * 80)
    for rv in (short, long_):
        _exec(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, review_id, fired_at) "
                       "VALUES (?, 'no_response', ?, datetime('now'))", (rid, rv))
    viewer = {"id": 1, "restaurant_id": rid, "role": "owner", "is_admin": 1}
    out, _ = client_api._do_get_notifications(rid, viewer)
    rows = {r["review_id"]: r for r in out["notifications"]}
    assert rows[short]["draft_complete"] is True
    assert rows[short]["draft_hash"] == models.draft_hash("Thanks so much, Ann!")
    assert rows[long_]["draft_complete"] is False and len(rows[long_]["draft"]) == 600
    assert rows[long_]["draft_hash"] == models.draft_hash("Thank you. " * 80)


def test_an_approve_by_hash_posts_only_the_reply_that_was_read(db_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None, raising=False)
    rid = _rid(db_path)
    text = "Thank you. " * 80
    rv = _drafted(db_path, rid, text)
    seen = models.draft_hash(text)
    # Changed since the row was read: nothing is approved.
    _exec(db_path, "UPDATE reviews SET draft_response=? WHERE id=?", (text + "Come back soon.", rv))
    payload, status = client_api._do_approve(rv, rid, auto=False, expected_draft_hash=seen)
    assert status == 409 and payload["draft_changed"] is True
    assert _q(db_path, "SELECT response_status FROM reviews WHERE id=?", (rv,))[0]["response_status"] == "drafted"
    # The reply as read: approved.
    _exec(db_path, "UPDATE reviews SET draft_response=? WHERE id=?", (text, rv))
    claimed = []
    real = models.claim_approval

    def spy(*a, **k):
        claimed.append(k.get("expected_draft"))
        return real(*a, **k)
    monkeypatch.setattr(models, "claim_approval", spy)
    payload, status = client_api._do_approve(rv, rid, auto=False, expected_draft_hash=seen.upper())
    assert payload["ok"] is True, payload
    # Held to the exact text that hashed (a compare-and-set, not a re-read).
    assert claimed == [text]
    assert _q(db_path, "SELECT response_status FROM reviews WHERE id=?", (rv,))[0]["response_status"] \
        in ("approved", "posted")


def test_both_approve_routes_read_the_hash():
    capi = open(os.path.join(ROOT, "client_api.py"), encoding="utf-8").read()
    web = capi[capi.index('@client_bp.route("/approve/<int:rid>"'):]
    web = web[:web.index("\n\n\n")]
    assert 'expected_draft_hash=_body.get("expected_draft_hash")' in web
    assert 'expected_draft=_body.get("expected_draft")' in web
    mob = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    route = mob[mob.index("def mobile_approve_review("):]
    route = route[:route.index("\n\n\n")]
    assert 'expected_draft_hash=_body.get("expected_draft_hash")' in route
    # The web bell sends it, and a clipped reply is never approved from a row.
    dash = _dash()
    fn = dash[dash.index("function approveRow(key, btn) {"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "expected_draft_hash: n.draft_hash" in fn
    assert "n.draft_complete !== false" in dash


# ── 11. the in-memory registries are on the workers plan's inventory ──────

def test_the_new_process_local_state_is_inventoried():
    plan = open(os.path.join(ROOT, "docs/plans/POSTGRES_AND_WORKERS_PLAN.md"), encoding="utf-8").read()
    for name in ("schedule_engine._GEN_WATCHERS", "push._provider_alarms", "schedule_engine._studio_cache",
                 "fire_silent", "live_activities", "insight_refresh"):
        assert name in plan, name
    assert re.search(r"_GEN_WATCHERS.*\|", plan)
