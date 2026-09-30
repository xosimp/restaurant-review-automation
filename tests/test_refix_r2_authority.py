"""Memory re-audit fix round 9/29/26, workstream R2 — every answer carries who
gave it, and only the restaurant's own answers teach the restaurant's learners.

  PEOPLE-3      rec_ledger.record derives `authority` from the request's login
                when a caller forgot the kwarg: a manager's "Not for us" no
                longer silences the whole restaurant or trains the owner.
  PLATFORM-2    the pooled recommendation-loop features count only the
                restaurant's own answers (never an admin's, never a
                delegate's decline).
  PLATFORM-3    an Ask confirm/dismiss records its authority; the pooled sync
                skips an admin's and a delegate's decline.
  PLATFORM-4    intel_rec_events carries authority; a delegate's decline is
                never filed as the restaurant's.
  QUALITY-19    the learning scorecard's Ask helpful rate leaves admin
                ratings out.
  PEOPLE-6      only the owner ends a goal.
  LOOPS-14 /    a manager's ranking reads their own declines, never another
  PEOPLE-16     manager's.
"""
import ast
import glob
import json
import os

import pytest
from flask import Flask, g

import models
import rec_ledger as rl
import rec_learning
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, name="Authority Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"),
                             db_path=db_path)


def _one(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def _owner(rid, uid=11):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}


def _manager(rid, uid=12):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": "dana"}


def _view_as(rid, uid=11, admin_id=99):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik",
            "acting_admin_id": admin_id, "acting_admin": "support-sam", "acting_admin_role": "support",
            "device_type": "admin-view-as"}


class _Req:
    """A request whose auth decorator resolved `user` (auth._bind_log_context
    keeps it on flask.g; a view-as puts its context on g.view_as too)."""

    def __init__(self, user):
        self.user = user
        self.app = Flask(__name__)

    def __enter__(self):
        self.ctx = self.app.test_request_context("/api/x", method="POST")
        self.ctx.__enter__()
        g.cavnar_current_user = self.user
        if self.user and self.user.get("acting_admin_id"):
            g.view_as = {"acting_admin_id": self.user["acting_admin_id"], "acting_admin": self.user["acting_admin"],
                         "acting_admin_role": self.user["acting_admin_role"]}
        return self

    def __exit__(self, *exc):
        self.ctx.__exit__(*exc)


# ── PEOPLE-3: authority derived where a caller forgot it ─────────────────────

def test_a_managers_answer_without_the_kwarg_is_the_managers(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db_path)
    with _Req(_manager(rid)):
        assert rl.record(rid, "trim_day:Tuesday", "dismissed", user_id=12, meta={"kind": "not_for_us"},
                         db_path=db_path)
    ev = _one(db_path, "SELECT authority FROM rec_events WHERE event='dismissed'")
    assert ev["authority"] == "delegate"
    # The owner is still shown it; only the manager's own view is silenced.
    assert "trim_day:Tuesday" not in rl.silenced_keys(rid, db_path=db_path)
    assert "trim_day:Tuesday" in rl.silenced_keys(rid, db_path=db_path, viewer=12)
    # Three of a manager's "not for us" never become the owner's declined
    # subjects (decisions.declined_subjects reads principal answers only).
    for day in ("Wednesday", "Thursday"):
        rl.present(rid, f"trim_day:{day}", "labor", "home", db_path=db_path)
        with _Req(_manager(rid)):
            rl.record(rid, f"trim_day:{day}", "dismissed", user_id=12, meta={"kind": "not_for_us"},
                      db_path=db_path)
    import decisions
    assert decisions.declined_subjects(rid, db_path=db_path) == []


def test_an_owners_answer_without_the_kwarg_is_the_principals(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db_path)
    with _Req(_owner(rid)):
        rl.record(rid, "trim_day:Tuesday", "dismissed", user_id=11, meta={"kind": "not_for_us"}, db_path=db_path)
    assert _one(db_path, "SELECT authority FROM rec_events WHERE event='dismissed'")["authority"] == "principal"
    assert "trim_day:Tuesday" in rl.silenced_keys(rid, db_path=db_path)


def test_a_view_as_answer_without_the_kwarg_is_the_admins_and_holds_for_the_admin(db_path):
    rid = _rid(db_path)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db_path)
    with _Req(_view_as(rid)):
        rl.record(rid, "reprice:Soup", "dismissed", user_id=11, meta={"kind": "not_for_us"}, db_path=db_path)
    assert _one(db_path, "SELECT authority FROM rec_events WHERE event='dismissed'")["authority"] == "admin"
    assert "reprice:Soup" not in rl.silenced_keys(rid, db_path=db_path)
    assert "reprice:Soup" in rl.silenced_keys(rid, db_path=db_path, viewer=99)


def test_an_admin_console_login_answering_is_kept_and_silences_nothing_for_the_owner(db_path):
    rid = _rid(db_path)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db_path)
    admin = {"id": 1, "is_admin": 1, "role": "admin", "username": "will"}
    with _Req(admin):
        # No via, no user_id: the silence holds for the signed-in admin, and
        # the answer is recorded rather than failing on a missing subject.
        assert rl.record(rid, "reprice:Soup", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    assert _one(db_path, "SELECT authority FROM rec_events WHERE event='dismissed'")["authority"] == "admin"
    assert "reprice:Soup" not in rl.silenced_keys(rid, db_path=db_path)
    assert "reprice:Soup" in rl.silenced_keys(rid, db_path=db_path, viewer=1)


def test_a_job_with_no_login_is_a_system_answer(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "completed", db_path=db_path)
    assert _one(db_path, "SELECT authority FROM rec_events WHERE event='completed'")["authority"] is None


def test_implemented_on_a_callers_connection_derives_too(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        with _Req(_manager(rid)):
            assert rl.implemented_on(conn, rid, "trim_day:Tuesday", "schedule", user_id=12) == 1
        conn.commit()
    finally:
        conn.close()
    assert _one(db_path, "SELECT authority FROM rec_events WHERE event='implemented'")["authority"] == "delegate"


# Every rec_ledger answer call that names no authority, with why the
# derivation (rec_ledger.request_authority) covers it. A new call site must be
# added here — with its reason — or pass authority= itself (PEOPLE-3).
_DERIVED = {
    "action_queue.py": "queue snooze route — the request's login",
    "admin_routes.py": "mark-posted (a login_required client route)",
    "client_api.py": "client routes — the request's login",
    "decisions.py": "restore_kind route — the request's login",
    "guest_marketing.py": "marketing routes (request) and the send job (system)",
    "home_brief.py": "delegate-to-issue accept route — the request's login",
    "intraday.py": "intraday route — the request's login",
    "issues.py": "issue resolved in a route (request) or a job (system)",
    "marketing.py": "marketing send routes (request) and jobs (system)",
    "marketing_opportunities.py": "campaign send route — the request's login",
    "menu_intelligence.py": "reprice apply route — the request's login",
    "mobile_api.py": "notification open route — the request's login",
    "rec_delivery.py": "open on a delivered push — the request's login",
    "schedule_engine.py": "schedule apply (request) or nightly draft (system)",
    "schedule_intel.py": "schedule outcome check — a job (system)",
    "schedule_versions.py": "schedule save on the caller's connection — implemented_on derives",
    "staffing_signals.py": "record_published — only the client_api schedule publish route (request's login)",
    "strategy_routes.py": "strategy routes — the request's login",
}


def test_every_answer_call_without_authority_is_covered_by_the_derivation():
    missing = {}
    files = glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "dsr", "*.py")) \
        + glob.glob(os.path.join(ROOT, "intelligence", "*.py"))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        if rel == "rec_ledger.py":
            continue
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("record", "implemented", "implemented_on")
                    and isinstance(node.func.value, ast.Name)):
                continue
            alias = node.func.value.id
            if "rl" not in alias.lower() and "rec_ledger" not in alias:
                continue
            if "authority" in {k.arg for k in node.keywords}:
                continue
            if rel not in _DERIVED:
                missing.setdefault(rel, []).append(node.lineno)
    assert not missing, f"rec_ledger answers with no authority and no entry in _DERIVED: {missing}"


# ── PLATFORM-2: the feature row counts the restaurant's answers only ─────────

def test_the_rec_loop_features_leave_out_admin_answers_and_delegate_declines(db_path):
    from datetime import date, timedelta
    from intelligence import features
    rid = _rid(db_path)
    for k in ("reprice:Soup", "reprice:Salad", "reprice:Steak", "reprice:Fish"):
        rl.present(rid, k, "food", "home", db_path=db_path)
    rl.record(rid, "reprice:Soup", "dismissed", via={"admin_id": 9}, meta={"kind": "not_for_us"}, db_path=db_path)
    rl.record(rid, "reprice:Salad", "dismissed", user_id=12, authority="delegate", meta={"kind": "not_for_us"},
              db_path=db_path)
    rl.record(rid, "reprice:Steak", "completed", user_id=12, authority="delegate", db_path=db_path)
    rl.record(rid, "reprice:Fish", "dismissed", authority="principal", meta={"kind": "not_for_us"}, db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        # `end` a day ahead: the ledger stamps UTC, which can be tomorrow here.
        out = features._rec_loop(conn, rid, date.today() - timedelta(days=28), date.today() - timedelta(days=90),
                                 end=date.today() + timedelta(days=1))
    finally:
        conn.close()
    # The owner's decline and the manager's Done (it closed the episode).
    assert out["recs_answered_28d"] == 2
    assert out["recs_declined_28d"] == 1 and out["recs_done_28d"] == 1


# ── PLATFORM-3/-4: the pooled sync ───────────────────────────────────────────

def test_the_sync_files_no_delegate_decline_and_carries_authority(db_path):
    from intelligence import feedback
    rid = _rid(db_path)
    for k in ("reprice:Soup", "reprice:Salad"):
        rl.present(rid, k, "food", "home", db_path=db_path)
    rl.record(rid, "reprice:Soup", "dismissed", user_id=12, authority="delegate", meta={"kind": "not_for_us"},
              db_path=db_path)
    rl.record(rid, "reprice:Salad", "completed", user_id=12, authority="delegate", db_path=db_path)
    feedback.sync(db_path=db_path)
    rows = models.get_conn(db_path).execute(
        "SELECT source_key, action, authority FROM intel_rec_events WHERE restaurant_id=?", (rid,)).fetchall()
    assert not [r for r in rows if "Soup" in r["source_key"]], "a manager's decline is not the restaurant's"
    salad = [r for r in rows if "Salad" in r["source_key"]]
    assert salad and salad[0]["authority"] == "delegate"


def test_an_ask_confirm_through_view_as_never_reaches_pooled_learning(db_path):
    from intelligence import feedback
    rid = _rid(db_path)
    pid = models.log_ask_action(rid, "send_supplier_order", summary="Send the order", outcome="proposed",
                                user_id=11, db_path=db_path)
    with _Req(_view_as(rid)):
        models.log_ask_action(rid, "send_supplier_order", summary="Send the order", outcome="confirmed",
                              user_id=99, proposal_id=pid, db_path=db_path)
    with _Req(_manager(rid)):
        models.log_ask_action(rid, "send_supplier_order", summary="Send the order", outcome="dismissed",
                              user_id=12, proposal_id=pid, db_path=db_path)
    rows = models.get_conn(db_path).execute(
        "SELECT outcome, authority, acting_admin_id FROM ask_cavnar_actions WHERE outcome != 'proposed' "
        "ORDER BY id").fetchall()
    assert [(r["outcome"], r["authority"], r["acting_admin_id"]) for r in rows] == [
        ("confirmed", "admin", 99), ("dismissed", "delegate", None)]
    feedback.sync(db_path=db_path)
    n = models.get_conn(db_path).execute("SELECT COUNT(*) FROM intel_rec_events WHERE restaurant_id=? "
                                         "AND synced_from='ask_cavnar_actions'", (rid,)).fetchone()[0]
    assert n == 0


def test_the_ask_action_route_stamps_authority_on_the_ledger_copy(db_path, monkeypatch):
    import client_api
    rid = _rid(db_path)
    pid = models.log_ask_action(rid, "send_supplier_order", summary="Send the order", outcome="proposed",
                                user_id=11, db_path=db_path)
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    user = _view_as(rid)
    with _Req(user):
        payload, status = client_api._do_record_ask_action(
            rid, 99, {"action": "send_supplier_order", "outcome": "dismissed", "proposal_id": pid}, user=user)
    assert status == 200
    ev = _one(db_path, "SELECT authority FROM rec_events WHERE event='dismissed'")
    assert ev["authority"] == "admin"


# ── QUALITY-19 ───────────────────────────────────────────────────────────────

def test_the_scorecards_ask_rate_leaves_admin_ratings_out(db_path):
    from datetime import date
    import learning_scorecard
    rid = _rid(db_path)
    conn = models.get_conn(db_path)
    for i, (helpful, auth) in enumerate(((1, "principal"), (0, "admin"), (0, "admin"))):
        conn.execute("INSERT INTO ask_feedback (restaurant_id, user_id, message_id, helpful, authority) "
                     "VALUES (?,?,?,?,?)", (rid, 11 + i, 100 + i, helpful, auth))
    conn.commit()
    conn.close()
    out = learning_scorecard.compute_month(rid, date.today().replace(day=1), db_path=db_path)
    assert out["metrics"]["ask_helpful_rate"] == {"value": 1.0, "k": 1, "n": 1}


# ── PEOPLE-6: only the owner ends a goal ─────────────────────────────────────

def test_only_the_owner_ends_a_goal(db_path):
    import goals
    rid = _rid(db_path)
    g = goals.set_goal(rid, "labor_pct", 27, user_id=11, authority="principal", db_path=db_path)
    assert goals.end_goal(rid, g["id"], user_id=12, authority="delegate", db_path=db_path) is False
    assert goals.end_goal(rid, g["id"], user_id=11, authority="admin", db_path=db_path) is False
    assert _one(db_path, "SELECT status FROM owner_goals WHERE id=?", g["id"])["status"] == "active"
    assert goals.end_goal(rid, g["id"], user_id=11, authority="principal", db_path=db_path) is True


def test_the_goal_end_route_refuses_a_manager_and_support(db_path, monkeypatch):
    import goals
    import metrics
    import strategy_routes
    real = models.get_conn
    for mod in (goals, metrics, strategy_routes):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(goals, "DB_PATH", db_path, raising=False)
    rid = _rid(db_path)
    g = goals.set_goal(rid, "labor_pct", 27, user_id=11, authority="principal", db_path=db_path)
    monkeypatch.setattr(strategy_routes, "_metric_visible", lambda u, m: True)
    for who in (_manager(rid), _view_as(rid)):
        body, status = strategy_routes._do_goal_end(who, g["id"])
        assert status == 403 and "owner" in body["error"]
    assert _one(db_path, "SELECT status FROM owner_goals WHERE id=?", g["id"])["status"] == "active"
    body, status = strategy_routes._do_goal_end(_owner(rid), g["id"])
    assert status == 200
    assert _one(db_path, "SELECT status FROM owner_goals WHERE id=?", g["id"])["status"] == "abandoned"


# ── LOOPS-14 / PEOPLE-16: one manager's "no" is theirs ──────────────────────

def test_a_managers_view_reads_only_their_own_declines(db_path):
    rid = _rid(db_path)
    for day in ("Tuesday", "Wednesday"):
        rl.present(rid, f"trim_day:{day}", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "dismissed", user_id=12, authority="delegate",
              meta={"kind": "not_for_us", "reason_code": "too_costly"}, db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        a = {e["key"]: e for e in rec_learning._load(conn, rid, perspective="delegate", viewer_id=12)}
        b = {e["key"]: e for e in rec_learning._load(conn, rid, perspective="delegate", viewer_id=13)}
        owner = {e["key"]: e for e in rec_learning._load(conn, rid)}
    finally:
        conn.close()
    assert a["trim_day:Tuesday"]["state"] == "dismissed"
    assert b["trim_day:Tuesday"]["state"] == "open", "manager B's advice is not reshaped by A's no"
    assert owner["trim_day:Tuesday"]["state"] in ("open", "delegated")
    # The reasons too: A's "too costly" is A's.
    mine = rec_learning.effectiveness(rid, db_path=db_path, perspective="delegate", viewer_id=12)
    theirs = rec_learning.effectiveness(rid, db_path=db_path, perspective="delegate", viewer_id=13)
    assert mine.reason_penalties("trim_day", [])[0] > 0
    assert theirs.reason_penalties("trim_day", [])[0] == 0


def test_the_viewer_of_a_login():
    assert rec_learning.viewer_of({"id": 12, "role": "manager"}) == 12
    assert rec_learning.viewer_of({"id": 11, "role": "client"}) is None
    assert rec_learning.viewer_of({"id": 11, "is_admin": 1}) is None
