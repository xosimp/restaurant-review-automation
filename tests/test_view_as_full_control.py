"""The owner's grant of full control (9/30/26, Erik to Will: "he's giving me
full control as we knock out stuff together"): until the date, an ADMIN's
writable view-as on that restaurant counts as the owner's — while the
admin behind each write is still the one recorded."""
import pytest

import auth
import models
import permissions
from auth import create_user, get_session_user, init_auth, sql_utc
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, auth):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


def _setup(db_path, days=30):
    rid = create_restaurant(Restaurant(name="Simple Co", owner_email="o@x.test"), db_path=db_path)
    owner = create_user(rid, "erik", "o@x.test", "Owner-pass-2026", db_path=db_path)
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    sup = create_user(hq, "sam", "s@x.test", "Support-pass-2026", role="support", db_path=db_path)
    if days is not None:
        from datetime import datetime, timedelta, timezone
        models.update_restaurant(rid, {"admin_control_until": sql_utc(datetime.now(timezone.utc) + timedelta(days=days)),
                                       "admin_control_note": "Erik, in person"}, db_path=db_path)
    return rid, owner, admin, sup


def test_an_admin_view_as_under_the_grant_counts_as_the_owner(db_path):
    rid, owner, admin, sup = _setup(db_path)
    u = get_session_user(auth.create_view_as_session(owner, {"id": admin, "is_admin": 1}, read_only=False,
                                                     db_path=db_path), db_path=db_path)
    assert u["view_as_full_control"] is True
    assert permissions.answer_authority(u) == "principal" and permissions.acting_via(u) is None
    assert permissions.acting_login_id(u) == admin                     # the admin is still who did it
    # what a rating or target write made this way is stamped with
    attribution = models._write_attribution(u)
    assert attribution == {"user_id": admin, "authority": "principal", "via": None}


def test_no_grant_expired_grant_support_or_read_only_stays_supports(db_path):
    rid, owner, admin, sup = _setup(db_path, days=None)
    tok = auth.create_view_as_session(owner, {"id": admin, "is_admin": 1}, read_only=False, db_path=db_path)
    u = get_session_user(tok, db_path=db_path)
    assert u["view_as_full_control"] is False and permissions.answer_authority(u) == "admin"
    models.update_restaurant(rid, {"admin_control_until": "2020-01-01 00:00:00"}, db_path=db_path)
    assert get_session_user(tok, db_path=db_path)["view_as_full_control"] is False
    from datetime import datetime, timedelta, timezone
    models.update_restaurant(rid, {"admin_control_until": sql_utc(datetime.now(timezone.utc) + timedelta(days=5))},
                             db_path=db_path)
    s = get_session_user(auth.create_view_as_session(owner, {"id": sup, "is_admin": 0}, read_only=False,
                                                     db_path=db_path), db_path=db_path)
    assert s["view_as_full_control"] is False and permissions.answer_authority(s) == "admin"
    ro = get_session_user(auth.create_view_as_session(owner, {"id": admin, "is_admin": 1}, read_only=True,
                                                      db_path=db_path), db_path=db_path)
    assert ro["view_as_full_control"] is False


def test_the_console_grants_with_a_note_and_ends_it_with_an_audit_row(db_path):
    import admin_routes
    rid, owner, admin, sup = _setup(db_path, days=None)
    fn = admin_routes.admin_api_full_control
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    from flask import Flask
    app = Flask(__name__)
    who = {"id": admin, "username": "will", "is_admin": 1}
    with app.test_request_context(json={"days": 30}):
        assert fn(rid, current_user=who)[1] == 400                     # who gave it is required
    with app.test_request_context(json={"days": 120, "note": "Erik"}):
        assert fn(rid, current_user=who)[1] == 400
    with app.test_request_context(json={"days": 30, "note": "Erik, in person, 9/30/26"}):
        resp, status = fn(rid, current_user=who)
        assert status == 200 and resp.get_json()["until"]
    assert models.get_restaurant(rid, db_path=db_path).admin_control_note == "Erik, in person, 9/30/26"
    with app.test_request_context(json={"days": 0}):
        assert fn(rid, current_user=who)[1] == 200
    assert models.get_restaurant(rid, db_path=db_path).admin_control_until is None
    conn = models.get_conn(db_path)
    acts = [r[0] for r in conn.execute("SELECT event_type FROM admin_events WHERE restaurant_id=? AND source='admin' ORDER BY id", (rid,))]
    conn.close()
    assert acts[-2:] == ["full_control.set", "full_control.end"]
