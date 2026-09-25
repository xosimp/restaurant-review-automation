"""is_demo — the flag that gates whether a restaurant's data can be
automatically wiped and reseeded. Before this existed, the boot-time Gia Mia
refresh ran unconditionally against any restaurant named "Gia Mia" with no
way to stop it once that restaurant became a real client."""
from flask import Flask

import admin_routes
from models import create_restaurant, get_restaurant, update_restaurant, Restaurant


def test_create_restaurant_defaults_is_demo_false(db_path):
    rid = create_restaurant(Restaurant(name="Real Client", owner_email="r@x.com"), db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 0


def test_create_restaurant_can_set_is_demo_true(db_path):
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 1


def test_update_restaurant_can_flip_is_demo(db_path):
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)
    update_restaurant(rid, {"is_demo": 0}, db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 0


def _delete(rid, body, monkeypatch, db_path):
    import models
    monkeypatch.setattr(models, "get_restaurant", lambda r, *a, **k: get_restaurant(r, db_path=db_path))
    real = models.delete_restaurant
    monkeypatch.setattr(models, "delete_restaurant", lambda r, *a, **k: real(r, db_path=db_path))
    app = Flask(__name__)
    with app.test_request_context(json=body):
        resp = admin_routes.admin_api_delete_demo.__wrapped__(rid, current_user={"is_admin": 1, "username": "will"})
    return (resp[0], resp[1]) if isinstance(resp, tuple) else (resp, 200)


def test_delete_demo_refuses_a_real_client(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Real Client", owner_email="r@x.com", is_demo=0), db_path=db_path)
    resp, code = _delete(rid, {"confirm_name": "Real Client"}, monkeypatch, db_path)
    assert code == 400 and resp.get_json()["ok"] is False
    assert get_restaurant(rid, db_path=db_path) is not None


def test_delete_demo_needs_the_exact_name(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)
    resp, code = _delete(rid, {"confirm_name": "demo co"}, monkeypatch, db_path)
    assert code == 400 and get_restaurant(rid, db_path=db_path) is not None


def test_delete_demo_removes_the_account_and_its_rows(db_path, monkeypatch):
    import models
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)
    keep = create_restaurant(Restaurant(name="Keep Co", owner_email="k@x.com"), db_path=db_path)
    conn = models.get_conn(db_path)
    for r in (rid, keep):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at) "
                     "VALUES (?, 'google', ?, 'A', 5, 'ok', datetime('now'))", (r, f"rv-{r}"))
    conn.commit()
    conn.close()
    resp, code = _delete(rid, {"confirm_name": "Demo Co"}, monkeypatch, db_path)
    assert code == 200 and resp.get_json()["ok"] is True and resp.get_json()["rows"] >= 2
    assert get_restaurant(rid, db_path=db_path) is None
    assert get_restaurant(keep, db_path=db_path) is not None
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=?", (keep,)).fetchone()[0] == 1
    conn.close()


def test_the_gia_mia_demo_stays_gone():
    """Removed 9/25/26: the boot seed recreated "Gia Mia" (with a default
    password) whenever its login was missing, and two admin routes wrote its
    seed data into any restaurant id. None of it may come back."""
    import os
    import demo_seed
    import models
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("_seed_gia_mia", "_do_seed_gia_mia", "_refresh_gia_mia_reviews",
                 "_seed_gia_mia_background", "_ensure_gia_mia_vibe"):
        assert not hasattr(demo_seed, name) and not hasattr(models, name), name
    src = open(os.path.join(root, "demo_seed.py"), encoding="utf-8").read()
    assert 'name="Gia Mia"' not in src and "charthouse123" not in src
    routes = open(os.path.join(root, "admin_routes.py"), encoding="utf-8").read()
    assert "reseed-demo-data" not in routes and "seed-labor-history" not in routes
    assert "reseed-demo-data" not in open(os.path.join(root, "templates", "admin.html"), encoding="utf-8").read()
