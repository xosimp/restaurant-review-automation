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


def test_the_simple_ejs_seed_never_touches_the_live_account_of_the_same_name(db_path):
    """Erik's live account is also "Simple EJ's" (9/25/26). The demo seed
    finds its own row by name AND is_demo, so the live one is never seeded,
    and with no demo left it does not create another beside the live one."""
    import demo_seed
    live = create_restaurant(Restaurant(name=demo_seed.SIMPLE_EJS_NAME, owner_email="erik@x.test", is_demo=0),
                             db_path=db_path)
    assert demo_seed._seed_simple_ejs(db_path) is None
    import models
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM restaurants WHERE name=?", (demo_seed.SIMPLE_EJS_NAME,)).fetchone()[0]
    shifts = conn.execute("SELECT COUNT(*) FROM labor_daily_history WHERE restaurant_id=?", (live,)).fetchone()[0]
    conn.close()
    assert n == 1 and shifts == 0


def test_the_simple_ejs_seed_picks_the_demo_when_both_exist(db_path):
    import demo_seed
    live = create_restaurant(Restaurant(name=demo_seed.SIMPLE_EJS_NAME, owner_email="erik@x.test", is_demo=0),
                             db_path=db_path)
    demo = create_restaurant(Restaurant(name=demo_seed.SIMPLE_EJS_NAME, owner_email="d@x.test", is_demo=1),
                             db_path=db_path)
    assert demo_seed._seed_simple_ejs(db_path) == demo
    import models
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM labor_daily_history WHERE restaurant_id=?", (live,)).fetchone()[0] == 0
    conn.close()
    assert not (get_restaurant(live, db_path=db_path).hours_notes or "")
    assert get_restaurant(demo, db_path=db_path).hours_notes


def test_the_demo_login_is_erikdemo_and_an_old_erik_login_is_renamed(db_path, monkeypatch):
    """Erik may want "erik" for his live account (9/25/26): the demo's login
    is "erikdemo", and a demo still carrying the old "erik" is renamed in
    place — same user id, so its sessions and devices keep working."""
    import auth
    import demo_seed
    import models
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    monkeypatch.setenv("DEMO_PASSWORD", "DemoPass2026!")
    demo = create_restaurant(Restaurant(name=demo_seed.SIMPLE_EJS_NAME, owner_email="d@x.test", is_demo=1),
                             db_path=db_path)
    uid = auth.create_user(demo, "erik", "erik+demo@cavnar.ai", "OldPass2026!", db_path=db_path)
    demo_seed._ensure_ejs_login(demo, db_path)
    conn = models.get_conn(db_path)
    names = [r[0] for r in conn.execute("SELECT username FROM users WHERE restaurant_id=?", (demo,))]
    same = conn.execute("SELECT id FROM users WHERE username='erikdemo'").fetchone()[0]
    conn.close()
    assert names == ["erikdemo"] and same == uid
    assert auth.verify_password("erikdemo", "DemoPass2026!", db_path=db_path)
