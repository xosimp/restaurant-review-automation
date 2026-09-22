"""Edge cases in the settings and restaurant-config write paths (DATA audit).

What these protect:
  * Alert-contact saves (web and phone twins) keep one row per number under
    a double-clicked Save, keep the original SMS consent timestamp (the
    A2P/TCPA evidence), never erase consent a client simply did not re-send,
    and never leave a restaurant with zero contacts when a save fails half
    way. (DATA-24)
  * A whole-form settings save from a stale copy (a second device, an old
    tab) is refused instead of silently reverting the other device's change
    — including `never_say`, the only gate on auto-approved public replies —
    and raw `UPDATE restaurants` writers bump `row_version` so the check can
    see them. (DATA-28)
  * Home and Ask stop answering from a pre-write snapshot after a CSV upload
    or a settings save. (DATA-39)
  * The process-local caches are bounded. (DATA-32)
  * Admin resend-contract, resend-payment and resend-welcome are safe to
    double-click. (DATA-30, DATA-38)
  * The 2FA send-test route is rate limited, and a double-tap does not kill
    the code the owner already received. (DATA-42)

Every external sender (Resend, Twilio, Stripe, DocuSign, APNs) is stubbed;
the conftest also blocks the network. Tests marked xfail(strict=True) assert
the correct behaviour against a confirmed defect and flip to a failure the
day the defect is fixed, so the marker goes with the fix.
"""
import io
import sys
import threading
import time
import types
from datetime import datetime, timezone

import pytest
from flask import Flask

# Imported eagerly, BEFORE any test patches models.get_conn: a module first
# imported inside a test would bind that test's redirect and keep pointing at
# a deleted temp file for the rest of the run (see tests/test_alert_holds.py).
import admin_routes
import ask_cavnar
import auth
import auth_routes
import client_api
import emails
import home_brief
import labor
import mobile_api
import models
import notify
import ops
import push
import webhook_routes
from admin_routes import admin_bp
from auth import create_session, create_user, init_auth, set_user_role
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_restaurant
from webhook_routes import webhook_bp


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """Point every module that bound `get_conn` at this test's database.

    Most modules here do `from models import get_conn` at import, which a
    patch of models.get_conn never reaches (CLAUDE.md, "Bound imports"). So
    every loaded module whose get_conn IS the real models.get_conn gets the
    redirect, plus the named ones explicitly."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, auth_routes, client_api, mobile_api, admin_routes,
                webhook_routes, push, home_brief, labor):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in (models, auth, push, notify):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(webhook_bp)
    return flask_app


def _restaurant(name="Settings Co", **kw):
    kw.setdefault("owner_email", "owner@x.test")
    kw.setdefault("module_reviews", 1)
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(name=name, **kw))


def _owner(rid, username="owner"):
    """A real primary login with a real session token. The same token works
    as the web cookie and as the phone's bearer header."""
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse-1")
    return uid, create_session(uid)


def _web(app, token):
    c = app.test_client()
    c.set_cookie("session_token", token)
    return c


def _post_alerts(app, surface, token, payload):
    if surface == "web":
        return _web(app, token).post("/api/alert-settings", json=payload)
    return app.test_client().post("/mobile/api/account/alert-settings", json=payload,
                                  headers={"Authorization": f"Bearer {token}"})


def _contacts(rid):
    conn = models.get_conn()
    rows = conn.execute("SELECT phone, sms_consent, sms_consent_at FROM alert_contacts "
                        "WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _row_version(rid):
    conn = models.get_conn()
    v = conn.execute("SELECT COALESCE(row_version,0) FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    return int(v)


OWNER_PHONE = "+15125550100"
GM_PHONE = "+15125550101"
TWO_CONTACTS = [{"name": "Owner", "phone": OWNER_PHONE}, {"name": "GM", "phone": GM_PHONE}]


def _alert_payload(contacts=TWO_CONTACTS, **extra):
    body = {"contacts": contacts, "urgent_via_sms": 1, "sms_consent": True,
            "alert_1star": 1, "alert_2star": 1, "digest_enabled": 1}
    body.update(extra)
    return body


# ── DATA-24 · alert contacts ────────────────────────────────────────────────

@pytest.mark.parametrize("surface", ["web", "mobile"])
@pytest.mark.xfail(strict=True, reason="DATA-24: contact sync is read, delete-each, insert-each with no "
                                       "transaction or UNIQUE, so two concurrent saves leave 4 rows")
def test_a_double_clicked_alert_save_leaves_one_contact_per_number(app, surface, monkeypatch):
    rid = _restaurant()
    _uid, token = _owner(rid)
    assert _post_alerts(app, surface, token, _alert_payload()).status_code == 200

    # Both requests read the existing contacts, then both proceed: the exact
    # interleaving a double-click produces. A route that no longer reads the
    # list this way simply never meets the barrier and runs straight through.
    barrier = threading.Barrier(2)
    real_get = notify.get_alert_contacts

    def get_then_wait(*a, **k):
        rows = real_get(*a, **k)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return rows
    monkeypatch.setattr(notify, "get_alert_contacts", get_then_wait)

    statuses = []

    def save():
        statuses.append(_post_alerts(app, surface, token, _alert_payload()).status_code)
    threads = [threading.Thread(target=save) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    monkeypatch.setattr(notify, "get_alert_contacts", real_get)

    phones = sorted(c["phone"] for c in _contacts(rid))
    assert phones == sorted([OWNER_PHONE, GM_PHONE]), (
        f"a double-clicked Save left {phones}: every alert SMS now goes out twice")


@pytest.mark.parametrize("surface", ["web", "mobile"])
@pytest.mark.xfail(strict=True, reason="DATA-24: every save deletes and re-inserts contacts, restamping "
                                       "sms_consent_at and destroying the original consent evidence")
def test_resaving_alert_settings_keeps_the_original_consent_time(app, surface):
    rid = _restaurant()
    _uid, token = _owner(rid)
    assert _post_alerts(app, surface, token, _alert_payload()).status_code == 200

    # The consent was given months ago; that moment is the evidence.
    original = "2026-01-05T09:00:00"
    conn = models.get_conn()
    conn.execute("UPDATE alert_contacts SET sms_consent_at=? WHERE restaurant_id=?", (original, rid))
    conn.commit()
    conn.close()

    # The owner flips one unrelated switch and saves the same two numbers.
    assert _post_alerts(app, surface, token, _alert_payload(alert_5star=1)).status_code == 200

    rows = _contacts(rid)
    assert sorted(r["phone"] for r in rows) == sorted([OWNER_PHONE, GM_PHONE])
    assert [r["sms_consent_at"] for r in rows] == [original, original]


@pytest.mark.parametrize("surface", ["web", "mobile"])
@pytest.mark.xfail(strict=True, reason="DATA-24: a save that does not re-send sms_consent re-creates "
                                       "already-consented numbers without consent, so SMS silently stops")
def test_a_save_that_omits_sms_consent_does_not_erase_consent_already_given(app, surface):
    rid = _restaurant()
    _uid, token = _owner(rid)
    assert _post_alerts(app, surface, token, _alert_payload()).status_code == 200
    assert len(notify.get_alert_contacts(rid, sms_consent_only=True)) == 2

    # A client that sends the form without the consent flag (an older build,
    # a partial form) is not the number's owner withdrawing consent.
    body = _alert_payload(alert_5star=1)
    body.pop("sms_consent")
    assert _post_alerts(app, surface, token, body).status_code == 200

    consented = sorted(c["phone"] for c in notify.get_alert_contacts(rid, sms_consent_only=True))
    assert consented == sorted([OWNER_PHONE, GM_PHONE])


@pytest.mark.parametrize("surface", ["web", "mobile"])
@pytest.mark.xfail(strict=True, reason="DATA-24: contacts are deleted and committed before the new ones "
                                       "are inserted, so a failure part-way leaves zero contacts")
def test_an_alert_save_that_fails_part_way_leaves_the_old_contacts_in_place(app, surface):
    rid = _restaurant()
    _uid, token = _owner(rid)
    assert _post_alerts(app, surface, token, _alert_payload()).status_code == 200

    # The database refuses the one genuinely new number (a full disk, a lock
    # timeout). Done with a trigger so it holds however the save is written.
    new_phone = "+15125550199"
    conn = models.get_conn()
    conn.execute(f"""CREATE TRIGGER _edge_refuse_contact BEFORE INSERT ON alert_contacts
                     WHEN NEW.phone = '{new_phone}'
                     BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END""")
    conn.commit()
    conn.close()

    _post_alerts(app, surface, token, _alert_payload(
        contacts=[{"name": "Owner", "phone": OWNER_PHONE}, {"name": "New GM", "phone": new_phone}]))

    phones = sorted(c["phone"] for c in _contacts(rid))
    assert phones == sorted([OWNER_PHONE, GM_PHONE]), f"a failed save left {phones}"


# ── DATA-28 · stale whole-form saves ────────────────────────────────────────

def test_a_profile_save_carrying_the_current_version_still_saves(app):
    """The other half of the stale-form contract: a form that is current is
    never refused. Passes today (nothing is refused) and must keep passing."""
    rid = _restaurant()
    _uid, token = _owner(rid)
    v = _row_version(rid)
    resp = app.test_client().post(
        "/mobile/api/account/update-profile", headers={"Authorization": f"Bearer {token}"},
        json={"owner_name": "Erik", "never_say": "cheap", "row_version": v, "expected_version": v})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    r = get_restaurant(rid)
    assert (r.owner_name, r.never_say) == ("Erik", "cheap")


@pytest.mark.xfail(strict=True, reason="DATA-28: settings routes never pass expected_version, so a stale "
                                       "phone form silently reverts never_say saved from the web")
def test_a_settings_save_from_a_stale_form_is_refused(app):
    rid = _restaurant()
    _uid, token = _owner(rid)
    phone_loaded_at = _row_version(rid)          # the phone opens its profile sheet

    # Meanwhile, on the laptop, the owner bans a phrase from public replies.
    web = _web(app, token)
    r = web.post("/api/brand-voice", json={"voice_notes": "warm", "never_say": "free dessert"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert get_restaurant(rid).never_say == "free dessert"

    # The phone saves the whole sheet it loaded before that change.
    resp = app.test_client().post(
        "/mobile/api/account/update-profile", headers={"Authorization": f"Bearer {token}"},
        json={"owner_name": "Erik", "voice_notes": "warm", "never_say": None,
              "row_version": phone_loaded_at, "expected_version": phone_loaded_at})

    assert get_restaurant(rid).never_say == "free dessert", \
        "the stale form reverted the banned-phrase list that gates auto-published replies"
    assert resp.status_code == 409


@pytest.mark.xfail(strict=True, reason="DATA-28: settings routes never pass expected_version, so a stale "
                                       "alert form from a second device reverts the first device's change")
def test_an_alert_settings_save_from_a_stale_form_is_refused(app):
    rid = _restaurant()
    _uid, token = _owner(rid)
    loaded_at = _row_version(rid)
    assert _post_alerts(app, "web", token, _alert_payload(alert_5star=1)).status_code == 200
    assert get_restaurant(rid).alert_5star == 1

    body = _alert_payload(alert_5star=0, row_version=loaded_at, expected_version=loaded_at)
    resp = _post_alerts(app, "mobile", token, body)

    assert get_restaurant(rid).alert_5star == 1
    assert resp.status_code == 409


@pytest.mark.parametrize("path", ["/mobile/api/account/update-email", "/api/update-email"])
@pytest.mark.xfail(strict=True, reason="DATA-28: the change-email routes write restaurants.owner_email "
                                       "with a raw UPDATE that skips the row_version bump")
def test_a_raw_restaurant_writer_bumps_the_row_version(app, path, monkeypatch):
    """A writer that bypasses update_restaurant is invisible to the stale-form
    check: a form loaded before it would still look current."""
    from auth_routes import auth_bp
    app.register_blueprint(auth_bp)
    monkeypatch.setattr(emails, "send_email_changed_email", lambda *a, **k: None)
    rid = _restaurant()
    _uid, token = _owner(rid)
    before = _row_version(rid)

    if path.startswith("/mobile"):
        resp = app.test_client().post(path, headers={"Authorization": f"Bearer {token}"},
                                      json={"new_email": "new@x.test", "current_password": "correct-horse-1"})
    else:
        resp = _web(app, token).post(path, json={"new_email": "new@x.test",
                                                 "current_password": "correct-horse-1"})
    assert resp.get_json()["ok"] is True
    assert get_restaurant(rid).owner_email == "new@x.test"
    assert _row_version(rid) > before


# ── DATA-39 · Home and Ask caches after a write ─────────────────────────────

def _prime_caches(rid, uid):
    home_brief._CACHE[(rid, uid)] = (datetime.now(timezone.utc), {"stale": True})
    ask_cavnar._CONTEXT_CACHE[(rid, ())] = (time.time(), "STALE SNAPSHOT")


def _still_cached(rid, uid):
    return {
        "home": (rid, uid) in home_brief._CACHE,
        "ask": any(k[0] == rid for k in ask_cavnar._CONTEXT_CACHE),
    }


SHIFTS_CSV = ("date,employee,role,actual_hours,sales\n"
              "2026-09-14,Sofia R.,Server,6,4000\n"
              "2026-09-14,Marcus T.,Cook,8,4000\n")


@pytest.mark.xfail(strict=True, reason="DATA-39: a CSV upload never invalidates the Home brief or the Ask "
                                       "context, so both answer from pre-upload data for up to a minute")
def test_a_csv_upload_invalidates_home_and_ask_for_that_restaurant(app, monkeypatch):
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {})
    rid = _restaurant()
    uid, token = _owner(rid)
    _prime_caches(rid, uid)

    resp = _web(app, token).post(
        "/client/upload-data",
        data={"data_type": "shifts", "csv_file": (io.BytesIO(SHIFTS_CSV.encode()), "shifts.csv")},
        content_type="multipart/form-data")
    assert resp.get_json()["ok"] is True

    assert _still_cached(rid, uid) == {"home": False, "ask": False}


@pytest.mark.xfail(strict=True, reason="DATA-39: a settings save never invalidates the Home brief or the "
                                       "Ask context")
def test_a_settings_save_invalidates_home_and_ask_for_that_restaurant(app):
    rid = _restaurant()
    uid, token = _owner(rid)
    _prime_caches(rid, uid)

    resp = _web(app, token).post("/api/account/profile",
                                 json={"owner_name": "Ada", "timezone": "America/Denver"})
    assert resp.get_json()["ok"] is True

    assert _still_cached(rid, uid) == {"home": False, "ask": False}


# ── DATA-32 · process-local caches are bounded ─────────────────────────────

_MANY = 20_000


@pytest.mark.xfail(strict=True, reason="DATA-32: home_brief._CACHE is a plain dict that is never evicted")
def test_the_home_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(home_brief, "_build", lambda user: ({"ok": True}, 200))
    monkeypatch.setattr(home_brief, "_CACHE", {})
    for i in range(_MANY):
        home_brief.build_home_brief({"restaurant_id": i, "id": 1})
    assert len(home_brief._CACHE) < _MANY


@pytest.mark.xfail(strict=True, reason="DATA-32: ask_cavnar._CONTEXT_CACHE is a plain dict that is never "
                                       "evicted")
def test_the_ask_context_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(ask_cavnar, "_CONTEXT_CACHE", {})
    for name in ("_identity_context", "_profile_context", "_memory_context", "_decisions_context",
                 "_intelligence_context", "_alerts_context", "_commitments_context",
                 "_cross_module_context"):
        monkeypatch.setattr(ask_cavnar, name, lambda *a, **k: "")
    for i in range(_MANY):
        ask_cavnar.build_context(types.SimpleNamespace(id=i, google_place_id=None))
    assert len(ask_cavnar._CONTEXT_CACHE) < _MANY


@pytest.mark.xfail(strict=True, reason="DATA-32: client_api._insight_cache is a plain dict that is never "
                                       "evicted")
def test_the_insight_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(client_api, "_insight_cache", {})
    for i in range(_MANY):
        client_api._cache_set(f"labor-insight:{i}", "narrative")
    assert len(client_api._insight_cache) < _MANY


@pytest.mark.xfail(strict=True, reason="DATA-32: a location switch calls home_brief.invalidate() with no "
                                       "id, clearing every restaurant's cached brief")
def test_a_location_switch_does_not_clear_other_restaurants_home_briefs(app):
    a = _restaurant("Syrup Lakeview", location_group="Syrup", owner_email="ann@a.test")
    b = _restaurant("Syrup Wicker Park", location_group="Syrup", owner_email="ann@a.test")
    stranger = _restaurant("Unrelated Diner", owner_email="zed@z.test")
    uid, token = _owner(a, "ann")
    set_user_role(uid, "owner")
    home_brief._CACHE[(stranger, 777)] = (datetime.now(timezone.utc), {"theirs": True})

    resp = _web(app, token).post("/api/switch-location", json={"restaurant_id": b})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True

    assert (stranger, 777) in home_brief._CACHE


# ── DATA-30 · resend-contract then sign the first envelope ──────────────────

def _admin_client(app):
    admin_rid = _restaurant("Cavnar HQ", owner_email="will@x.test")
    uid = create_user(admin_rid, "will", "will@x.test", "admin-pass-1", is_admin=True)
    return _web(app, create_session(uid))


@pytest.mark.xfail(strict=True, reason="DATA-30: resend-contract overwrites docusign_envelope_id, so a "
                                       "signature on the first envelope matches no restaurant")
def test_a_signature_on_an_earlier_envelope_still_completes_onboarding(app, monkeypatch):
    import docusign_helper
    monkeypatch.delenv("DOCUSIGN_WEBHOOK_SECRET", raising=False)
    envelopes = iter(["env-first", "env-second"])
    monkeypatch.setattr(docusign_helper, "send_contract",
                        lambda **k: {"envelope_id": next(envelopes)})
    rid = _restaurant("Simple EJ's", owner_email="erik@ej.test")
    create_user(rid, "erik", "erik@ej.test", "client-pass-1")
    admin = _admin_client(app)

    # The admin double-clicks Resend contract.
    for _ in range(2):
        assert admin.post(f"/admin/resend-contract/{rid}").get_json()["ok"] is True

    # The client opens the first email and signs that envelope.
    resp = app.test_client().post("/docusign/webhook",
                                  json={"envelopeId": "env-first", "status": "completed"})
    assert resp.status_code == 200

    assert get_restaurant(rid).contract_status == "signed"


# ── DATA-38 · resend-payment and resend-welcome ─────────────────────────────

def _fake_stripe(monkeypatch):
    """A stand-in for the stripe package that records what would have been
    created. Nothing reaches Stripe."""
    created, expired = [], []

    class _Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Product:
        @staticmethod
        def search(**k):
            return _Obj(data=[_Obj(id="prod_1")])

        @staticmethod
        def create(**k):
            return _Obj(id="prod_1")

    class Price:
        @staticmethod
        def create(**k):
            return _Obj(id="price_%d" % (len(created) + 1))

    class Session:
        @staticmethod
        def create(**k):
            sid = "cs_%d" % (len(created) + 1)
            created.append({"id": sid, "idempotency_key": k.get("idempotency_key")})
            return _Obj(id=sid, url="https://checkout.test/" + sid)

        @staticmethod
        def expire(sid, **k):
            expired.append(sid)

        @staticmethod
        def list(**k):
            return _Obj(data=[], auto_paging_iter=lambda: iter([]))

    fake = types.ModuleType("stripe")
    fake.api_key = None
    fake.Product, fake.Price = Product, Price
    fake.checkout = types.SimpleNamespace(Session=Session)
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_edge")
    return created, expired


@pytest.mark.xfail(strict=True, reason="DATA-38: every resend-payment click creates two new Stripe "
                                       "checkout sessions with no idempotency key; all stay live")
def test_resend_payment_reuses_the_open_checkout_session(app, monkeypatch):
    created, expired = _fake_stripe(monkeypatch)
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test_edge")
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda **k: sent.append(k))
    rid = _restaurant("Simple EJ's", owner_email="erik@ej.test")
    admin = _admin_client(app)

    for _ in range(2):
        assert admin.post(f"/admin/resend-payment/{rid}").get_json()["ok"] is True
    assert len(sent) == 2 and created, "the stub did not see the checkout being built"

    # One live session per billing period, however many times it was clicked.
    distinct = {c["idempotency_key"] or c["id"] for c in created}
    live = {s for s in distinct if s not in expired}
    assert len(live) <= 2, f"{len(live)} payable checkout links are live for one client"


@pytest.mark.xfail(strict=True, reason="DATA-38: every resend-welcome click resets the password, so the "
                                       "password in the first email no longer works")
def test_a_double_clicked_resend_welcome_leaves_the_first_emailed_password_working(app, monkeypatch):
    emailed = []
    monkeypatch.setattr(emails, "send_welcome_email", lambda **k: emailed.append(k["password"]))
    rid = _restaurant("Simple EJ's", owner_email="erik@ej.test")
    create_user(rid, "erik", "erik@ej.test", "client-pass-1")
    admin = _admin_client(app)

    for _ in range(2):
        assert admin.post(f"/admin/resend-welcome/{rid}").get_json()["ok"] is True
    assert emailed, "the stub did not see the welcome email"

    assert auth.verify_password("erik", emailed[0]), \
        "the client opens the first email and its password is already dead"


# ── DATA-42 · 2FA send-test ─────────────────────────────────────────────────

@pytest.mark.parametrize("surface", ["web", "mobile"])
@pytest.mark.xfail(strict=True, reason="DATA-42: the 2FA send-test route has no limiter; every call "
                                       "sends a paid SMS")
def test_send_test_2fa_is_rate_limited(app, surface, monkeypatch):
    texts = []
    monkeypatch.setattr(notify, "send_2fa_sms", lambda phone, name, code: texts.append(code) or True)
    rid = _restaurant(owner_phone="+15125550100")
    _uid, token = _owner(rid)

    statuses = []
    for _ in range(10):
        if surface == "web":
            r = _web(app, token).post("/api/account/2fa/send-test", json={"method": "sms"})
        else:
            r = app.test_client().post("/mobile/api/account/2fa/send-test", json={"method": "sms"},
                                       headers={"Authorization": f"Bearer {token}"})
        statuses.append(r.status_code)

    assert texts, "the stub never saw a text, so the route did not run"
    assert 429 in statuses
    assert len(texts) < 10


@pytest.mark.xfail(strict=True, reason="DATA-42: each send-test call overwrites two_fa_code, so a "
                                       "double-tap kills the code the owner already received")
def test_a_double_tapped_send_test_leaves_the_first_code_usable(app, monkeypatch):
    texts = []
    monkeypatch.setattr(notify, "send_2fa_sms", lambda phone, name, code: texts.append(code) or True)
    rid = _restaurant(owner_phone="+15125550100")
    _uid, token = _owner(rid)
    hdr = {"Authorization": f"Bearer {token}"}
    c = app.test_client()

    for _ in range(2):
        c.post("/mobile/api/account/2fa/send-test", json={"method": "sms"}, headers=hdr)
    assert texts

    resp = c.post("/mobile/api/account/2fa/verify", json={"code": texts[0]}, headers=hdr)
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
