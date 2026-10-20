"""The guest list a page at a time, and its two counts from the database
(scale audit 10/19/26 FE-10): the web fetched the whole club to count it and
rendered every contact as one HTML string."""
import pytest
from flask import Flask

import auth
import client_api
import models
from client_api import client_bp
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=db_path)
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(guest_marketing, "DB_PATH", db_path)


@pytest.fixture
def client(monkeypatch, db_path):
    rid = create_restaurant(Restaurant(name="Club Co", owner_email="c@x.com", module_marketing=1), db_path=db_path)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0})
    conn = models.get_conn(db_path)
    for i in range(1, 251):
        conn.execute("INSERT INTO guest_contacts (restaurant_id, name, phone, consent, unsubscribed) VALUES (?,?,?,?,?)",
                     (rid, f"Guest {i}", f"+1312555{i:04d}", 1 if i % 2 else 0, 1 if i % 10 == 1 else 0))
    conn.commit()
    conn.close()
    app = Flask(__name__)
    app.register_blueprint(client_bp)
    return app.test_client()


def test_the_counts_match_the_whole_list(client):
    whole = client.get("/api/guest-contacts").get_json()["contacts"]
    counts = client.get("/api/guest-contacts/counts").get_json()
    assert counts["total"] == len(whole) == 250
    assert counts["textable"] == len([c for c in whole if c["consent"] and not c["unsubscribed"]])


def test_pages_walk_the_whole_list_newest_first(client):
    whole = [c["id"] for c in client.get("/api/guest-contacts").get_json()["contacts"]]
    seen, before = [], None
    while True:
        d = client.get("/api/guest-contacts?limit=100" + (f"&before={before}" if before else "")).get_json()
        assert len(d["contacts"]) <= 100
        seen += [c["id"] for c in d["contacts"]]
        before = d["next_before"]
        if not before:
            break
    assert seen == whole


def test_search_matches_a_name_or_the_phones_digits(client):
    client.post("/api/guest-contacts", json={"name": "Maria Lopez", "phone": "(773) 555-0101"})
    by_name = client.get("/api/guest-contacts?limit=50&q=LOPEZ").get_json()["contacts"]
    assert [c["name"] for c in by_name] == ["Maria Lopez"]
    by_phone = client.get("/api/guest-contacts?limit=50&q=(312) 555-0007").get_json()["contacts"]
    assert [c["name"] for c in by_phone] == ["Guest 7"]


def test_the_web_counts_and_pages_instead_of_fetching_everything():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "dashboard.html"), encoding="utf-8").read()
    summary = src.split("function loadGuestContactsSummary() {", 1)[1].split("\n}\n", 1)[0]
    assert "/api/guest-contacts/counts" in summary and "d.contacts" not in summary
    lst = src.split("function _loadGuestContactsList(before) {", 1)[1].split("\n}\n", 1)[0]
    assert "'/api/guest-contacts?limit=' + GUEST_PAGE" in lst and "next_before" in lst
