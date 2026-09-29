"""Code the docs wave found wrong (9/29/26): a fresh database's memberships
had no person_id until the second boot, and a deleted restaurant's
per-login and location preferences outlived it."""
import os
import sqlite3
import tempfile

import auth
import models


def _fresh():
    p = os.path.join(tempfile.mkdtemp(), "t.db")
    models.init_db(p)
    auth.init_auth(p)
    return p


def test_memberships_get_person_id_on_the_first_boot():
    p = _fresh()
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(memberships)")}
    assert "person_id" in cols


def test_deleting_a_restaurant_removes_its_preferences_only():
    p = _fresh()
    import preferences
    preferences.init_preferences(p)
    conn = sqlite3.connect(p)
    conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('Gone', 'g@x.test')")
    conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('Stays', 's@x.test')")
    gone, stays = [r[0] for r in conn.execute("SELECT id FROM restaurants ORDER BY id")]
    for scope, sid in (("location", str(gone)), ("login", f"9:{gone}"), ("location", str(stays)),
                       ("login", f"9:{stays}"), ("login", f"9:{gone}1")):
        conn.execute("INSERT INTO preferences (scope, scope_id, key, value) VALUES (?,?,?,?)",
                     (scope, sid, "k", "1"))
    conn.commit()
    conn.close()
    models.delete_restaurant(gone, db_path=p)
    left = {tuple(r) for r in sqlite3.connect(p).execute("SELECT scope, scope_id FROM preferences")}
    assert left == {("location", str(stays)), ("login", f"9:{stays}"), ("login", f"9:{gone}1")}
