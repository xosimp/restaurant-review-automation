"""Data layer and migrations at the edges (DATA audit: DATA-1, DATA-11, DATA-21,
DATA-43, DATA-44, DATA-55, DATA-60, DATA-61).

What this protects: the SQLite schema, its boot migrations and the model
functions every surface reads through. The edges here are the ones that
turn one failure into a platform-wide one — a database refusing writes, a
lock held at boot, a migration pointed at the wrong file — and the ones
where the data layer quietly loses or leaks data: an import that stores
nothing but says "imported", a delete that always fails, a retired review
still sent to the model, a restaurant that silently drops out of every job.

Every test that calls init_db runs from a scratch working directory:
init_db's ensure_columns() call writes to the def-time default ./reviews.db
(DATA-44), and a test must never touch the developer's real database.

Tests without a marker pin behaviour that works today; xfail(strict=True)
tests assert the correct behaviour for a defect the audit confirmed.
"""
import inspect
import io
import os
import re
import sqlite3
import sys

import pytest
from flask import Flask

import auth
import client_api
import models
import notify
import ops
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, Review, create_restaurant, save_reviews

# Imported at collection so each binds the real get_conn, not a test's redirect.
import analyser, schedule_versions  # noqa: E401,F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_GET_CONN = models.get_conn          # captured at import, before any fixture patches it
CSRF = "edge-data-layer-csrf"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch, tmp_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    redirect._edge_redirect = True
    for mod in list(sys.modules.values()):
        g = getattr(mod, "get_conn", None) if mod is not None else None
        if g is real or getattr(g, "_edge_redirect", False):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    scratch = tmp_path / "cwd"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    init_auth(db_path=db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Layer Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


# ── a database that refuses writes (DATA-1) ────────────────────────────────

@pytest.fixture
def web(db_path):
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    rid = _rid(db_path)
    uid = create_user(rid, "owner", "o@x.test", "a-Long-unique-pass-9481!", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    client = app.test_client()
    client.set_cookie("csrf_js", CSRF)
    client.set_cookie("session_token", create_session(uid, db_path=db_path))
    return {"client": client, "rid": rid, "uid": uid}


def test_an_authenticated_read_works_on_a_healthy_database(web):
    r = web["client"].get("/api/home/brief?fresh=1")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]


def test_a_database_refusing_writes_still_serves_authenticated_reads(web, db_path, monkeypatch):
    base = sqlite3.connect

    def refuses(*a, **k):
        conn = base(db_path, timeout=1, factory=models._TrackedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")      # SQLITE_FULL / read-only volume, as RECOVERY "Volume full"
        return conn
    monkeypatch.setattr(auth, "get_conn", refuses)
    r = web["client"].get("/api/home/brief?fresh=1")
    assert r.status_code != 500, "RECOVERY.md promises 'writes fail, reads succeed'"
    assert r.status_code == 200


# ── boot migrations under a lock (DATA-11) ─────────────────────────────────

def _columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="DATA-11: every boot migration swallows any exception, so 'database is "
                                       "locked' is treated as 'column exists' and init_db returns on a drifted schema")
def test_a_locked_database_fails_the_boot_migration_loudly(tmp_path, monkeypatch):
    path = str(tmp_path / "boot.db")
    models.init_db(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE forecast_log DROP COLUMN signed_error_pct")   # "the new release adds a column"
    conn.commit()
    conn.close()

    holder = sqlite3.connect(path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")                  # an overlapped container / worker.py / railway ssh sqlite3
    holder.execute("UPDATE restaurants SET name=name")
    real_connect = sqlite3.connect
    # Shrink every busy wait so the test takes milliseconds, not the 5 s default.
    monkeypatch.setattr(sqlite3, "connect", lambda p, *a, **k: real_connect(p, *a, **{**k, "timeout": 0.1}))
    try:
        try:
            models.init_db(path)
            raised = False
        except Exception:
            raised = True
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)
        holder.rollback()
        holder.close()
    migrated = "signed_error_pct" in _columns(path, "forecast_log")
    assert raised or migrated, "init_db returned normally and the app would serve on the drifted schema"


@pytest.mark.xfail(strict=True, reason="DATA-11: hosted_dashboard wraps every init_* in one try/except that prints "
                                       "'DB init error' and carries on, so a failed boot is promoted as healthy")
def test_a_failed_boot_init_stops_the_process():
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    at = src.index('print(f"DB init error: {_e}")')
    handler_start = src.rindex("except Exception as _e:", 0, at)
    lines = src[handler_start:].splitlines()[1:]
    body = []
    for ln in lines:
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
        body.append(ln)
    block = "\n".join(body)
    assert re.search(r"\braise\b|sys\.exit|os\._exit|SystemExit", block), \
        f"the boot init handler swallows the failure:\n{block}"


# ── init_db's reach (DATA-44) ──────────────────────────────────────────────

def _default_db_path():
    return inspect.signature(models.ensure_columns).parameters["db_path"].default


@pytest.mark.xfail(strict=True, reason="DATA-44: init_db(db_path) calls ensure_columns() with no argument, so it "
                                       "migrates the default ./reviews.db as well as the database it was given")
def test_init_db_touches_only_the_database_it_was_given(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "get_conn", _REAL_GET_CONN)   # the redirect would hide where it writes
    default = _default_db_path()
    if os.path.isabs(default):
        pytest.skip("default DB_PATH is absolute in this environment; cannot observe it safely")
    here = os.path.abspath(default)
    assert here.startswith(str(tmp_path)), "the scratch cwd fixture is not in effect — refusing to run"
    target = str(tmp_path / "given.db")
    models.init_db(target)
    assert not os.path.exists(here), f"init_db({target!r}) also created/migrated {here}"


# ── third-party review imports (DATA-21) ───────────────────────────────────

@pytest.fixture
def importer(db_path, monkeypatch):
    import threading
    monkeypatch.setattr(threading, "Thread", _Inert)
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    rid = _rid(db_path, name="Import Co")
    uid = create_user(rid, "imp", "imp@x.test", "a-Long-unique-pass-9481!", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    client = app.test_client()
    client.set_cookie("csrf_js", CSRF)
    client.set_cookie("session_token", create_session(uid, db_path=db_path))

    def post(csv_text, platform=None):
        data = {"file": (io.BytesIO(csv_text.encode()), "reviews.csv")}
        if platform:
            data["platform"] = platform
        return client.post("/api/import-tripadvisor", data=data, content_type="multipart/form-data",
                           headers={"X-CSRF": CSRF})
    return {"post": post, "rid": rid}


class _Inert:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        pass

    def start(self):
        pass


def _stored(db_path, rid):
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    return n


TA_CSV = "rating,text,author,date\n2,Cold food and slow service,Ann,2026-09-01\n5,Lovely pasta,Bob,2026-09-02\n"


def test_a_google_review_saves_through_the_same_path(db_path):
    rid = _rid(db_path)
    assert save_reviews([Review(restaurant_id=rid, platform="google", external_id="g1", author="a",
                                rating=2, text="cold")], db_path=db_path)[0] == 1


@pytest.mark.xfail(strict=True, reason="DATA-21: reviews.platform CHECK allows only google/yelp/csv/manual, so "
                                       "every TripAdvisor/DoorDash/UberEats row is rejected while the route reports ok")
@pytest.mark.parametrize("platform", [None, "doordash", "ubereats"])
def test_a_third_party_import_stores_what_it_reports(importer, db_path, platform):
    body = importer["post"](TA_CSV, platform=platform).get_json()
    stored = _stored(db_path, importer["rid"])
    assert body["ok"] is True, body
    assert stored == body["imported"] == 2, f"route said imported={body.get('imported')}, stored {stored}"


@pytest.mark.xfail(strict=True, reason="DATA-21: nothing stored but the route still answers ok=True — a refusal "
                                       "is never reported to the owner")
def test_an_import_that_stores_nothing_is_not_reported_as_success(importer, db_path):
    body = importer["post"](TA_CSV).get_json()
    if _stored(db_path, importer["rid"]) == 0:
        assert body["ok"] is False, f"nothing was stored and the owner was told {body}"


@pytest.mark.xfail(strict=True, reason="DATA-21: the import's external_id is hash(text) (salted per process) plus "
                                       "the row index, so a re-upload after a deploy duplicates every review")
def test_a_reimport_after_a_restart_dedupes(importer, db_path, monkeypatch):
    import builtins
    assert importer["post"](TA_CSV).get_json()["ok"] is True
    first = _stored(db_path, importer["rid"])
    # A new process salts str hashes differently; stand in for that inside the route's module.
    monkeypatch.setattr(client_api, "hash", lambda v: builtins.hash(("another-process", v)), raising=False)
    body = importer["post"](TA_CSV).get_json()
    assert first == 2, "the first import stored nothing"
    assert _stored(db_path, importer["rid"]) == 2 and body.get("new") == 0, body


# ── deleting a generated schedule (DATA-55) ────────────────────────────────

def _history_with_version(db_path, rid):
    import schedule_versions as sv
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
    hid = models.save_schedule_history(rid, "2026-09-28", "2026-10-04", 10.0, 20.0, 30.0, csv, [], db_path=db_path)
    sv.append(rid, hid, "generated", csv, saved_by="Cavnar AI", db_path=db_path)   # every generation does this
    return hid


def test_a_schedule_row_without_versions_can_be_deleted(db_path):
    rid = _rid(db_path)
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
    hid = models.save_schedule_history(rid, "2026-09-28", "2026-10-04", 10.0, 20.0, 30.0, csv, [], db_path=db_path)
    assert models.delete_schedule_history(hid, rid, db_path=db_path) is True


def test_a_generated_schedule_with_versions_can_be_deleted(db_path):
    rid = _rid(db_path)
    hid = _history_with_version(db_path, rid)
    assert models.delete_schedule_history(hid, rid, db_path=db_path) is True
    conn = models.get_conn(db_path)
    left = conn.execute("SELECT COUNT(*) FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    conn.close()
    assert left == 0


def test_a_failed_schedule_delete_does_not_leave_the_write_lock_held(db_path):
    rid = _rid(db_path)
    hid = _history_with_version(db_path, rid)
    try:
        models.delete_schedule_history(hid, rid, db_path=db_path)
    except Exception:
        other = sqlite3.connect(db_path, timeout=0.1)
        try:
            other.execute("BEGIN IMMEDIATE")        # would raise 'database is locked' if the lock leaked
            other.rollback()
        finally:
            other.close()


# ── retired reviews (DATA-60) ──────────────────────────────────────────────

def _retired_negative(db_path, rid):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="old-1", author="a", rating=1,
                         text="Terrible, never again")], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET deleted_at=datetime('now','-1 day'), fetched_at=datetime('now','-5 days') "
                 "WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()


@pytest.mark.xfail(strict=True, reason="DATA-60: get_pending_analysis does not filter deleted_at, so reviews the "
                                       "retention setting retired are still sent to the model")
def test_a_retired_review_is_not_sent_for_analysis(db_path):
    rid = _rid(db_path)
    _retired_negative(db_path, rid)
    assert models.get_pending_analysis(rid, db_path=db_path) == []


@pytest.mark.xfail(strict=True, reason="DATA-60: get_pending_drafts does not filter deleted_at")
def test_a_retired_review_is_not_drafted(db_path):
    rid = _rid(db_path)
    _retired_negative(db_path, rid)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET processed=1, response_status='pending', sentiment='negative' WHERE restaurant_id=?",
                 (rid,))
    conn.commit()
    conn.close()
    assert models.get_pending_drafts(rid, db_path=db_path) == []


def test_a_retired_review_does_not_raise_a_no_response_alert(db_path, monkeypatch):
    rid = _rid(db_path)
    models.update_restaurant(rid, {"alert_no_response": 1, "urgent_via_email": 1}, db_path=db_path)
    _retired_negative(db_path, rid)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET processed=1, response_status='pending', sentiment='negative' WHERE restaurant_id=?",
                 (rid,))
    conn.commit()
    conn.close()
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda *a, **k: raised.append(a[:2]))
    notify.check_no_response_alerts(db_path=db_path)
    assert raised == []


def test_a_live_unanswered_negative_does_raise_the_alert(db_path, monkeypatch):
    rid = _rid(db_path)
    models.update_restaurant(rid, {"alert_no_response": 1, "urgent_via_email": 1}, db_path=db_path)
    _retired_negative(db_path, rid)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET deleted_at=NULL, processed=1, response_status='pending', sentiment='negative' "
                 "WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda *a, **k: raised.append(a[:2]))
    notify.check_no_response_alerts(db_path=db_path)
    assert raised == [(rid, "no_response")]


# ── restaurant deletion (DATA-61) ──────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-61: there is no deletion routine; deletion_requested_at is a flag "
                                       "nothing acts on and 72 FKs make DELETE FROM restaurants fail")
def test_delete_restaurant_removes_every_row_in_every_table_with_a_restaurant_id(two_restaurants):
    db = two_restaurants["db_path"]
    a, b = two_restaurants["rid_a"], two_restaurants["rid_b"]
    for rid in (a, b):
        uid = create_user(rid, f"u{rid}", f"u{rid}@x.test", "a-Long-unique-pass-9481!", db_path=db)
        upsert_membership(uid, rid, "client", db_path=db)
        _history_with_version(db, rid)
    assert hasattr(models, "delete_restaurant"), "no deletion routine exists"
    models.delete_restaurant(a, db_path=db)

    conn = sqlite3.connect(db)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        leftovers, kept_b = [], 0
        for t in tables:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            if "restaurant_id" not in cols:
                continue
            n = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE restaurant_id=?", (a,)).fetchone()[0]
            if n:
                leftovers.append(f"{t}: {n}")
            kept_b += conn.execute(f"SELECT COUNT(*) FROM {t} WHERE restaurant_id=?", (b,)).fetchone()[0]
        assert not conn.execute("SELECT 1 FROM restaurants WHERE id=?", (a,)).fetchone()
    finally:
        conn.close()
    assert not leftovers, f"rows left behind for the deleted restaurant: {leftovers}"
    assert kept_b > 0, "the other restaurant's rows were removed too"


# ── a restaurant that fails to hydrate (DATA-43) ───────────────────────────

def test_one_malformed_row_does_not_cost_the_rest_of_the_list(db_path):
    good, bad = _rid(db_path, name="Good"), _rid(db_path, name="Bad")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET week_start_day='Monday' WHERE id=?", (bad,))
    conn.commit()
    conn.close()
    assert good in [r.id for r in models.get_all_restaurants(db_path=db_path)]


@pytest.mark.xfail(strict=True, reason="DATA-43: get_all_restaurants drops a row that fails to hydrate with a bare "
                                       "except/pass, so one client silently leaves every scheduled job")
def test_a_skipped_row_is_reported_to_ops(db_path, monkeypatch):
    bad = _rid(db_path, name="Bad")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET week_start_day='Monday' WHERE id=?", (bad,))
    conn.commit()
    conn.close()
    captured = []
    monkeypatch.setattr(ops, "capture", lambda exc, job="unknown", context="", **k: captured.append((job, context)))
    models.get_all_restaurants(db_path=db_path)
    assert any(str(bad) in (ctx or "") for _job, ctx in captured), captured


@pytest.mark.xfail(strict=True, reason="DATA-43: update_restaurant does not type-check, so one non-numeric write "
                                       "makes the restaurant vanish from get_all_restaurants")
def test_a_bad_settings_value_cannot_make_a_restaurant_vanish_from_every_job(db_path):
    rid = _rid(db_path)
    try:
        models.update_restaurant(rid, {"week_start_day": "Monday"}, db_path=db_path)
    except (ValueError, TypeError):
        pass                                           # refusing the write is also correct
    assert rid in [r.id for r in models.get_all_restaurants(db_path=db_path)]
