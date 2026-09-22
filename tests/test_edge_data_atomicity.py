"""Writes that span several commits must land whole or not at all, and a
check-then-write must have one winner when two requests race it.

What this protects (DATA audit, multi-commit atomicity):
  - two staff claiming one open shift: one winner, and the schedule and the
    request agree on who it is (DATA-17)
  - approve and deny racing on one shift request: one outcome (DATA-36)
  - a paid Stripe checkout whose login creation fails is still provisioned
    when Stripe retries the event (DATA-54)
  - a manager's schedule edit and its version row commit together, so the
    two-manager conflict check can see every save (DATA-57)
  - a staff name claim that fails part-way can be retried by the same
    employee (DATA-58)
  - a shift CSV upload whose follow-on writes fail is reported and does not
    half-apply; two concurrent self-signups leave one restaurant, not an
    orphan (DATA-62)

Tests without a marker pin behaviour that works today. Tests marked
xfail(strict=True) assert the CORRECT behaviour for a defect confirmed in the
DATA audit; they flip to a failure the day the defect is fixed, so the marker
is removed with the fix.

Races use real threads against the test's own SQLite file, with a gated
connection that holds each thread just before the contested statement until
both are there — so both requests are inside the race window on every run.
Every sender is stubbed; nothing reaches the network.
"""
import datetime as dt
import io
import sqlite3
import sys
import threading

import pytest
from flask import Flask

import auth
import client_api
import emails
import labor
import mobile_api
import models
import notify
import provisioning
import push
import schedule_versions
import shift_requests
import staff_settings
import webhook_routes
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "edge-data-atom-csrf"
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
CLEAN = (HEADER + f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,\n"
         f"{WEEK[1]},Tuesday,Bob,Server,4:00pm,10:00pm,6.0,\n")
TODAY = dt.date(2026, 9, 28)


# ── race gate (same shape as test_edge_data_idempotency.py) ─────────────────

_ACTIVE_GATE = {}


class _Gate:
    """Holds each thread that reaches a statement containing `needle` until
    `parties` threads are there, once per thread. A barrier timeout (the
    fixed code may never let the loser reach the statement) just lets the
    waiting thread carry on."""

    def __init__(self, needle, parties=2, timeout=1.0):
        self.needle = " ".join(needle.split()).lower()
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.local = threading.local()

    def reached(self, sql):
        if getattr(self.local, "done", False):
            return
        if self.needle in " ".join(str(sql).split()).lower():
            self.local.done = True
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                pass


class _GatedConn:
    def __init__(self, conn, gate):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_gate", gate)

    def execute(self, sql, *a, **k):
        self._gate.reached(sql)
        return self._conn.execute(sql, *a, **k)

    def executemany(self, sql, *a, **k):
        self._gate.reached(sql)
        return self._conn.executemany(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def _run_together(*fns):
    out = [None] * len(fns)

    def runner(i, fn):
        try:
            out[i] = fn()
        except BaseException as e:           # the loser of a race may raise
            out[i] = e

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "a racing thread never finished"
    return out


def _arm(needle, parties=2):
    _ACTIVE_GATE["gate"] = _Gate(needle, parties=parties)


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    """Every module that bound get_conn at import (CLAUDE.md "Bound
    imports") is pointed at this test's database — the named ones, and any
    other loaded module still holding the real function."""
    real = models.get_conn

    def redirect(*a, **k):
        conn = real(db_path)
        gate = _ACTIVE_GATE.get("gate")
        return _GatedConn(conn, gate) if gate else conn

    named = (models, auth, client_api, mobile_api, labor, notify, push, provisioning,
             schedule_versions, shift_requests, staff_settings, webhook_routes)
    for mod in named:
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, provisioning, schedule_versions, shift_requests, staff_settings):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [], raising=False)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    _ACTIVE_GATE.clear()
    yield
    _ACTIVE_GATE.clear()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Edge Atomic Co")
    kw.setdefault("owner_email", "owner@atomic.test")
    kw.setdefault("timezone", "America/Chicago")
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _week(db_path, rid, csv_text=CLEAN, published=True):
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
        "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?)",
        (rid, WEEK[0], WEEK[6], 12, 40, 30, csv_text, "2026-10-01 10:00:00" if published else None))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def _team(db_path, rid, names=("Ana", "Bob", "Cy", "Dee")):
    for n in names:
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)


def _csv(db_path, hid):
    conn = models.get_conn(db_path)
    text = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    conn.close()
    return text


# ── DATA-17 · claiming an open shift ────────────────────────────────────────

def test_two_concurrent_claims_on_one_open_shift_have_one_winner(db_path):
    rid = _restaurant(db_path)
    _team(db_path, rid)
    hid = _week(db_path, rid)
    schedule_versions.append(rid, hid, "published", CLEAN, db_path=db_path)
    req = shift_requests.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=TODAY)
    assert shift_requests.decide(rid, req["id"], True, decided_by="mgr", db_path=db_path)["status"] == "open"

    _arm("UPDATE shift_change_requests SET status='covered'")
    results = _run_together(lambda: shift_requests.claim(rid, req["id"], "Cy", db_path=db_path),
                            lambda: shift_requests.claim(rid, req["id"], "Dee", db_path=db_path))
    _ACTIVE_GATE.clear()

    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1, f"{len(winners)} people were told they got one shift"
    assert isinstance([r for r in results if not isinstance(r, dict)][0], shift_requests.ShiftRequestError)
    # The schedule and the request agree on who is working it.
    monday = next(r for r in schedule_versions.rows_from_csv(_csv(db_path, hid)) if r["date"] == WEEK[0])
    assert monday["employee"] == winners[0]["replacement_name"]


def test_a_claim_on_a_shift_already_covered_is_refused(db_path):
    rid = _restaurant(db_path)
    _team(db_path, rid)
    hid = _week(db_path, rid)
    schedule_versions.append(rid, hid, "published", CLEAN, db_path=db_path)
    req = shift_requests.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=TODAY)
    shift_requests.decide(rid, req["id"], True, db_path=db_path)
    assert shift_requests.claim(rid, req["id"], "Cy", db_path=db_path)["replacement_name"] == "Cy"
    with pytest.raises(shift_requests.ShiftRequestError):
        shift_requests.claim(rid, req["id"], "Dee", db_path=db_path)
    monday = next(r for r in schedule_versions.rows_from_csv(_csv(db_path, hid)) if r["date"] == WEEK[0])
    assert monday["employee"] == "Cy"


# ── DATA-36 · approve and deny from two tabs ────────────────────────────────

def test_approve_and_deny_race_has_one_outcome(db_path):
    rid = _restaurant(db_path)
    _team(db_path, rid)
    _week(db_path, rid)
    req = shift_requests.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=TODAY)

    _arm("UPDATE shift_change_requests SET status=?")
    results = _run_together(lambda: shift_requests.decide(rid, req["id"], True, decided_by="mgr1", db_path=db_path),
                            lambda: shift_requests.decide(rid, req["id"], False, decided_by="mgr2", db_path=db_path))
    _ACTIVE_GATE.clear()

    decided = [r for r in results if isinstance(r, dict)]
    assert len(decided) == 1, f"both managers were told their decision stood: {[r['status'] for r in decided]}"
    final = shift_requests.mine(rid, "Ana", db_path=db_path)[0]
    assert final["status"] == decided[0]["status"] and final["decided_by"] == decided[0]["decided_by"]


# ── DATA-54 · Stripe checkout provisioning ──────────────────────────────────

def _stripe_client(monkeypatch, event):
    """The stripe library stands in for its one call; the route is real."""
    import types
    fake = types.ModuleType("stripe")
    fake.Webhook = type("W", (), {"construct_event": staticmethod(lambda *a, **k: event)})
    monkeypatch.setitem(sys.modules, "stripe", fake)
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    return app.test_client()


@pytest.mark.xfail(strict=True, reason="DATA-54: the event is claimed before three separate provisioning commits, so a failed create_user leaves a restaurant with no login and Stripe's retry is dropped as a duplicate")
def test_a_checkout_whose_user_creation_fails_can_be_retried_by_stripe(db_path, monkeypatch):
    welcomed = []
    monkeypatch.setattr(emails, "send_welcome_email", lambda **kw: welcomed.append(kw["to_email"]))
    real_create_user = auth.create_user
    calls = {"n": 0}

    def create_user_locked_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_create_user(*a, **k)

    monkeypatch.setattr(auth, "create_user", create_user_locked_once)
    event = {"id": "evt_checkout_1", "type": "checkout.session.completed",
             "data": {"object": {"customer": "cus_new1", "subscription": "sub_1",
                                 "customer_details": {"email": "paid@owner.test", "name": "Pat Paid"},
                                 "metadata": {"restaurant": "Paid Place", "module_keys": "reviews"}}}}
    c = _stripe_client(monkeypatch, event)
    c.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t"})    # the lock hits
    c.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t"})    # Stripe's retry

    conn = models.get_conn(db_path)
    users = conn.execute("SELECT id FROM users WHERE LOWER(email)='paid@owner.test'").fetchall()
    places = conn.execute("SELECT id, billing_status FROM restaurants WHERE owner_email='paid@owner.test'").fetchall()
    conn.close()
    assert len(users) == 1, "the customer paid and has no login"
    assert len(places) == 1 and places[0]["billing_status"] == "active"
    assert welcomed == ["paid@owner.test"]


# ── DATA-57 · schedule edit save ────────────────────────────────────────────

def test_an_edit_and_its_version_commit_together(db_path, monkeypatch):
    rid = _restaurant(db_path)
    owner = create_user(rid, "mgr", "mgr@atomic.test", "pw-atomic-1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": True})
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda *a, **k: [])
    original = HEADER + "2026-09-12,Saturday,Sam,Bartender,5:00pm,11:00pm,6,"
    hid = models.save_schedule_history(rid, "2026-09-12", "2026-09-12", 6, 40, 28, original, [], db_path=db_path)
    schedule_versions.append(rid, hid, "generated", original, db_path=db_path)
    before = len(schedule_versions.list_versions(rid, hid, db_path=db_path))

    def version_write_fails(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(schedule_versions, "append", version_write_fails)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    rows = [{"date": "2026-09-12", "day": "Saturday", "employee": "Pat", "role": "Bartender",
             "shift_start": "5:00pm", "shift_end": "11:00pm", "scheduled_hours": "6", "notes": ""}]
    app.test_client().post("/mobile/api/labor/schedule/score",
                           json={"rows": rows, "save": True, "history_id": hid, "version": before},
                           headers={"Authorization": "Bearer " + create_session(owner, device_type="ios",
                                                                                db_path=db_path)})

    edited = "Pat" in _csv(db_path, hid)
    versioned = len(schedule_versions.list_versions(rid, hid, db_path=db_path)) > before
    assert edited == versioned, "the edit was saved with no version row, so a stale save from a second manager will overwrite it"


# ── DATA-58 · staff name claim ──────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-58: claim_staff_name is six separate commits, so a failure after the membership is written leaves the name taken with no PIN and the retry is refused")
def test_a_claim_that_fails_half_way_can_be_retried(db_path, monkeypatch):
    rid = _restaurant(db_path)
    models.init_manual_team_members(db_path)
    models.add_manual_team_member(rid, "Jordan P.", "Bartender", db_path=db_path)
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: True)
    started = auth.start_staff_signup("5550142233", optin=True, db_path=db_path)
    token = auth.verify_staff_signup("5550142233", started["dev_code"], db_path=db_path)

    real_set_pin = auth.set_membership_pin
    calls = {"n": 0}

    def set_pin_locked_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_set_pin(*a, **k)

    monkeypatch.setattr(auth, "set_membership_pin", set_pin_locked_once)
    with pytest.raises(sqlite3.OperationalError):
        auth.claim_staff_name(token, rid, "Jordan P.", "5063", db_path=db_path)

    done = auth.claim_staff_name(token, rid, "Jordan P.", "5063", db_path=db_path)   # the employee taps again
    assert done["employee_name"] == "Jordan P."
    assert auth.verify_membership_pin(done["membership_id"], rid, "5063", db_path=db_path)["ok"] is True


# ── DATA-62 · follow-on writes that half-apply ──────────────────────────────

SHIFTS_HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
FIRST_UPLOAD = (SHIFTS_HEADER
                + "2026-09-01,Tuesday,Ann,Server,11:00,17:00,6,6,4000,\n"
                + "2026-09-02,Wednesday,Bob,Cook,10:00,18:00,8,8,4200,\n")
SECOND_UPLOAD = (SHIFTS_HEADER
                 + "2026-09-08,Tuesday,Ann,Server,11:00,17:00,6,6,4100,\n"
                 + "2026-09-09,Wednesday,Bob,Cook,10:00,18:00,8,8,4300,\n")


def _upload(client, raw):
    return client.post("/client/upload-data",
                       data={"data_type": "shifts", "csv_file": (io.BytesIO(raw.encode()), "shifts.csv")},
                       content_type="multipart/form-data", headers={"X-CSRF": CSRF})


@pytest.mark.xfail(strict=True, reason="DATA-62: the shift CSV commits first and the daily-history write fails separately with only a print, so the upload half-applies and reports success")
def test_a_failed_history_write_is_reported_and_the_upload_is_atomic(db_path, monkeypatch):
    rid = _restaurant(db_path)
    owner = create_user(rid, "owner", "owner@atomic.test", "pw-atomic-1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    c = app.test_client()
    c.set_cookie("session_token", create_session(owner, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    assert _upload(c, FIRST_UPLOAD).get_json()["ok"] is True

    def history_write_fails(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(models, "save_labor_daily_history", history_write_fails)
    body = _upload(c, SECOND_UPLOAD).get_json()

    stored = (models.get_client_data(rid, db_path=db_path) or {}).get("shifts_csv")
    assert stored == FIRST_UPLOAD, "the new CSV is live while the daily history YoY generation reads is the old one"
    assert body.get("ok") is not True, "the upload reported success with its history write lost"


@pytest.mark.xfail(strict=True, reason="DATA-62: signup creates the restaurant in its own commit before create_user, so a double-tap leaves an orphan restaurant and a 500")
def test_two_concurrent_signups_with_one_email_leave_one_restaurant(db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "1")
    monkeypatch.setattr(emails, "send_signup_welcome_email", lambda *a, **k: True)
    monkeypatch.setattr(emails, "send_signup_admin_alert", lambda *a, **k: True)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    form = {"restaurant_name": "Double Tap Diner", "owner_name": "Dee", "email": "dee@signup.test",
            "username": "deesignup", "password": "long-enough-1"}

    _arm("INSERT INTO restaurants")
    statuses = _run_together(lambda: app.test_client().post("/mobile/api/register", json=form).status_code,
                             lambda: app.test_client().post("/mobile/api/register", json=form).status_code)
    _ACTIVE_GATE.clear()

    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM restaurants WHERE owner_email='dee@signup.test'").fetchone()[0]
    conn.close()
    assert n == 1, f"{n} restaurants for one signup — {n - 1} orphaned with no login"
    assert sorted(statuses) == [201, 409], statuses
