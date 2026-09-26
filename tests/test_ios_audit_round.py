"""iOS audit round (9/25/26), the server halves:

- the mobile review payload sends its SQLite 0/1 flags as real booleans
  (a number where the phone declared a Bool failed the whole inbox);
- an approve can carry the reply the owner approved (`expected_draft`), and
  a stored reply that differs is refused with nothing posted — the phone's
  offline queue must never post an older draft than the one approved;
- /mobile/api/me carries the restaurant's time zone, so the phone learns its
  restaurant clock after every sign-in, switch and launch.
"""
import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import guest_marketing
import mobile_api
import models
import push
from auth import create_user, init_auth
from auth_routes import _login_attempts
from mobile_api import mobile_bp
from models import Restaurant, Review, create_restaurant, get_conn, save_reviews, update_restaurant


@pytest.fixture(autouse=True)
def _tables(db_path):
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    push.init_push(db_path=db_path)
    guest_marketing.init_guest_marketing(db_path)
    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def client(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api, guest_marketing, push):
        monkeypatch.setattr(mod, "get_conn", redirect)
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _setup(client, db_path, draft="Thanks for coming, Ann!", needs_review=0, tz=None):
    rid = create_restaurant(Restaurant(name="Audit Co", owner_email="o@x.com"), db_path=db_path)
    if tz:
        update_restaurant(rid, {"timezone": tz}, db_path=db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="r1",
                         author="Ann", rating=2, text="Slow service.")], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET draft_response=?, response_status='drafted', processed=1, "
                 "draft_needs_review=?, draft_edited=1 WHERE restaurant_id=?", (draft, needs_review, rid))
    conn.commit()
    review_id = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    conn.close()
    create_user(rid, "owner1", "owner1@x.com", "correct-horse", db_path=db_path)
    token = client.post("/mobile/api/login", json={"username": "owner1", "password": "correct-horse"}).get_json()["token"]
    return rid, review_id, {"Authorization": f"Bearer {token}"}


def _status(db_path, review_id):
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()[0]
    finally:
        conn.close()


def test_the_mobile_review_payload_sends_its_flags_as_booleans(client, db_path):
    _rid, review_id, headers = _setup(client, db_path, needs_review=0)
    body = client.get("/mobile/api/reviews?limit=20", headers=headers).get_json()
    row = next(r for r in body["reviews"] if r["id"] == review_id)
    for key in ("draft_needs_review", "draft_edited", "processed", "can_retract"):
        assert isinstance(row[key], bool), f"{key} must be a JSON boolean, got {row[key]!r}"
    assert row["draft_needs_review"] is False and row["draft_edited"] is True
    one = client.get(f"/mobile/api/reviews/{review_id}", headers=headers).get_json()["review"]
    assert one["draft_needs_review"] is False


def test_a_flagged_draft_reads_true_not_one(client, db_path):
    _rid, review_id, headers = _setup(client, db_path, needs_review=1)
    rows = client.get("/mobile/api/reviews", headers=headers).get_json()["reviews"]
    assert next(r for r in rows if r["id"] == review_id)["draft_needs_review"] is True


def test_an_approve_of_a_reply_that_changed_is_refused_and_nothing_is_posted(client, db_path):
    _rid, review_id, headers = _setup(client, db_path, draft="The current reply.")
    resp = client.post(f"/mobile/api/reviews/{review_id}/approve", headers=headers,
                       json={"expected_draft": "The reply the owner read."})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False and body["draft_changed"] is True
    assert _status(db_path, review_id) == "drafted"


def test_an_approve_of_the_stored_reply_goes_through(client, db_path):
    _rid, review_id, headers = _setup(client, db_path, draft="The current reply.")
    # As the phone holds it: a save stores the text stripped.
    resp = client.post(f"/mobile/api/reviews/{review_id}/approve", headers=headers,
                       json={"expected_draft": "The current reply.\n"})
    assert resp.status_code == 200, resp.get_json()
    assert _status(db_path, review_id) in ("approved", "posted")


def test_an_approve_without_expected_draft_is_unchanged(client, db_path):
    _rid, review_id, headers = _setup(client, db_path)
    resp = client.post(f"/mobile/api/reviews/{review_id}/approve", headers=headers)
    assert resp.status_code == 200


def test_claim_approval_holds_the_expected_draft_in_its_where_clause(db_path):
    rid = create_restaurant(Restaurant(name="CAS Co", owner_email="c@x.com"), db_path=db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="c1",
                         author="Cy", rating=4, text="Good.")], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET draft_response='Kept.', response_status='drafted' WHERE restaurant_id=?", (rid,))
    conn.commit()
    review_id = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    conn.close()
    assert models.claim_approval(review_id, rid, db_path=db_path, expected_draft="Other.") is False
    assert models.claim_approval(review_id, rid, db_path=db_path, expected_draft="Kept.") is True


def test_me_carries_the_restaurants_time_zone(client, db_path):
    _rid, _review_id, headers = _setup(client, db_path, tz="America/Denver")
    body = client.get("/mobile/api/me", headers=headers).get_json()
    assert body["ok"] is True
    assert body["timezone"] == "America/Denver"
    assert body["user"]["restaurant_id"] == _rid
