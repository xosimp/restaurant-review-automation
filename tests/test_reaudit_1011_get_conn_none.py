"""models.get_conn(None) is the platform database (blind re-audit 10/11/26):
owner_rules threads db_path=None to models, and sqlite3.connect(None) made
every generation's staff-note read fail silently."""
import models


def test_get_conn_with_no_path_opens_the_platform_database(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    conn = models.get_conn(None)
    try:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM restaurants").fetchone()[0] >= 0
    finally:
        conn.close()


def test_owner_rules_staff_note_read_runs_with_no_db_path(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import owner_rules
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path) \
        if "db_path" in models.create_restaurant.__code__.co_varnames else models.create_restaurant(
            models.Restaurant(name="EJ", owner_email="e@x.test"))
    assert owner_rules._staff_note_parts(rid, db_path=None) == []


def test_a_staff_note_the_hold_reader_cannot_hold_is_read_with_the_real_get_conn(db_path, monkeypatch):
    # STRESS-17 (schedule fix round 10/11/26, J): the generation calls
    # _staff_note_parts(rid, None) through the REAL models.get_conn — with
    # sqlite3.connect(None) every note was silently dropped, so a test that
    # only saw [] passed on the broken code. A note that must come back.
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import owner_rules
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    models.save_staff_note(rid, "Ana", "Pair her with Bo whenever the patio is open", db_path=db_path)
    got = owner_rules._staff_note_parts(rid, db_path=None)
    assert ("Ana", "Pair her with Bo whenever the patio is open") in got
