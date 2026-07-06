"""SMS consent enforcement for alert_contacts — the gap that caused a real
compliance mismatch: the admin panel could enroll any phone number for SMS
with zero consent record, while the client-facing consent checkbox was
never actually transmitted to or checked by the server. Twilio's repeated
A2P 10DLC campaign rejections trace back to this exact inconsistency."""
from models import create_restaurant, Restaurant, get_conn
from notify import add_alert_contact, get_alert_contacts


def _redirect_db(monkeypatch, db_path):
    """notify.py, client_api.py, and admin_routes.py each did
    `from models import get_conn` at module level, so they hold their own
    bound copy independent of models.get_conn — patching only the latter
    (as ops.py's lazy-import style allows elsewhere in this suite) leaves
    these three writing to the real reviews.db instead of the test fixture.
    Every module that touches alert_contacts without an explicit db_path
    needs its own reference redirected."""
    import models, notify, client_api, admin_routes
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, notify, client_api, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect)


def test_add_alert_contact_defaults_to_no_consent(db_path):
    rid = create_restaurant(Restaurant(name="A", owner_email="a@x.com"), db_path=db_path)
    add_alert_contact(rid, "Owner", "+15551234567", db_path=db_path)
    contacts = get_alert_contacts(rid, db_path=db_path)
    assert contacts[0]["sms_consent"] is False


def test_add_alert_contact_records_explicit_consent(db_path):
    rid = create_restaurant(Restaurant(name="B", owner_email="b@x.com"), db_path=db_path)
    add_alert_contact(rid, "Owner", "+15551234567", sms_consent=True, db_path=db_path)
    contacts = get_alert_contacts(rid, db_path=db_path)
    assert contacts[0]["sms_consent"] is True
    conn = get_conn(db_path)
    row = conn.execute("SELECT sms_consent_at FROM alert_contacts WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["sms_consent_at"] is not None  # timestamped, auditable


def test_sms_consent_only_filters_out_non_consented(db_path):
    rid = create_restaurant(Restaurant(name="C", owner_email="c@x.com"), db_path=db_path)
    add_alert_contact(rid, "Consented", "+15551111111", sms_consent=True, db_path=db_path)
    add_alert_contact(rid, "Admin-added, no consent", "+15552222222", sms_consent=False, db_path=db_path)

    all_contacts = get_alert_contacts(rid, db_path=db_path)
    sms_eligible = get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)

    assert len(all_contacts) == 2          # management UI sees both
    assert len(sms_eligible) == 1          # only the consented one can be texted
    assert sms_eligible[0]["name"] == "Consented"


def test_save_alert_settings_downgrades_sms_without_consent(db_path, monkeypatch):
    """A direct API call requesting urgent_via_sms=1 without sms_consent=true
    must not enable SMS — the modal's client-side checkbox guard is a UX
    nicety, not enforcement; this is the actual enforcement."""
    import client_api
    _redirect_db(monkeypatch, db_path)
    rid = create_restaurant(Restaurant(name="E", owner_email="e@x.com"), db_path=db_path)

    class FakeReq:
        @staticmethod
        def get_json():
            return {
                "contacts": [{"name": "Owner", "phone": "+15551234567"}],
                "urgent_via_sms": 1,
                "sms_consent": False,  # box never checked
            }

    monkeypatch.setattr(client_api, "request", FakeReq)
    from flask import Flask
    with Flask(__name__).app_context():
        client_api.save_alert_settings.__wrapped__(current_user={"restaurant_id": rid})

    from models import get_restaurant
    r = get_restaurant(rid, db_path=db_path)
    assert r.urgent_via_sms == 0  # forced off despite the request asking for 1
    contacts = get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)
    assert contacts == []  # contact saved, but not SMS-eligible


def test_save_alert_settings_enables_sms_with_real_consent(db_path, monkeypatch):
    import client_api
    _redirect_db(monkeypatch, db_path)
    rid = create_restaurant(Restaurant(name="F", owner_email="f@x.com"), db_path=db_path)

    class FakeReq:
        @staticmethod
        def get_json():
            return {
                "contacts": [{"name": "Owner", "phone": "+15551234567"}],
                "urgent_via_sms": 1,
                "sms_consent": True,
            }

    monkeypatch.setattr(client_api, "request", FakeReq)
    from flask import Flask
    with Flask(__name__).app_context():
        client_api.save_alert_settings.__wrapped__(current_user={"restaurant_id": rid})

    from models import get_restaurant
    r = get_restaurant(rid, db_path=db_path)
    assert r.urgent_via_sms == 1
    contacts = get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)
    assert len(contacts) == 1


def test_admin_added_contact_can_never_receive_sms(db_path, monkeypatch):
    """The exact gap: admin_routes.add_alert_contact_route must never pass
    sms_consent=True, since an operator typing someone else's number in
    isn't that person consenting."""
    import admin_routes
    _redirect_db(monkeypatch, db_path)
    rid = create_restaurant(Restaurant(name="D", owner_email="d@x.com"), db_path=db_path)

    class FakeReq:
        @staticmethod
        def get_json():
            return {"name": "Some Manager", "phone": "+15553334444"}

    monkeypatch.setattr(admin_routes, "request", FakeReq)
    # Bypass the @admin_required decorator — call the underlying view directly.
    # jsonify() needs a Flask app context; a bare throwaway app supplies one.
    from flask import Flask
    with Flask(__name__).app_context():
        resp = admin_routes.add_alert_contact_route.__wrapped__(rid, current_user={"is_admin": 1})
        assert resp.get_json()["ok"] is True

    sms_eligible = get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)
    assert sms_eligible == []  # admin-added contact is NOT SMS-eligible
