"""A rating's change-log subject is the person (owner, 10/1/26).

The log used to write "Name · attribute", and the people stitcher reads that
column as a name store: "Gideon Kopalchick · overall" became a person of its
own, and Erik was asked "Is Antonio Corona Martinez · overall the same person
as Antonio Corona Martinez?" — 109 phantoms at Simple EJ's. The subject is
now the name, the attribute its own column, old rows are split at boot, a
labelled string is never a name, and the phantoms are removed.
"""
import pytest

import models
import people
from models import Restaurant, create_restaurant, get_capability_changes, record_capability_change


def _rows(db, sql, args=()):
    conn = models.get_conn(db)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


@pytest.fixture
def rid(db_path):
    models.init_capability_changes(db_path)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.com"), db_path=db_path)


def test_a_rating_is_logged_under_the_person_with_its_attribute_apart(db_path, rid):
    record_capability_change(rid, "rating", subject="Antonio Corona Martinez", attribute="overall",
                             before=None, after={"score": 4}, changed_by="erik", db_path=db_path)
    row = _rows(db_path, "SELECT subject, attribute FROM capability_changes WHERE restaurant_id=?", (rid,))[0]
    assert row == {"subject": "Antonio Corona Martinez", "attribute": "overall"}
    # the change log still reads the way it did
    shown = get_capability_changes(rid, db_path=db_path)[0]
    assert shown["subject"] == "Antonio Corona Martinez · overall" and shown["person"] == "Antonio Corona Martinez"


def test_old_labelled_subjects_are_split_once_at_boot(db_path, rid):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO capability_changes (restaurant_id, kind, subject) VALUES (?,?,?)",
                 (rid, "rating", "Evan Price · can_close"))
    conn.execute("INSERT INTO capability_changes (restaurant_id, kind, subject) VALUES (?,?,?)",
                 (rid, "threshold", "Bartender · min"))
    conn.commit()
    conn.close()
    models.init_capability_changes(db_path)
    models.init_capability_changes(db_path)
    got = _rows(db_path, "SELECT kind, subject, attribute FROM capability_changes WHERE restaurant_id=? ORDER BY id",
                (rid,))
    assert got == [{"kind": "rating", "subject": "Evan Price", "attribute": "can_close"},
                   {"kind": "threshold", "subject": "Bartender · min", "attribute": None}]


def test_a_labelled_string_is_never_a_person(db_path, rid):
    assert not people._looks_like_name("Gideon Kopalchick · overall")
    assert not people._looks_like_name("can_close")
    assert people._looks_like_name("Gideon Kopalchick")


def test_the_phantom_people_and_their_questions_are_removed(db_path, rid):
    people.init_people(db_path)
    conn = models.get_conn(db_path)
    try:
        real = conn.execute("INSERT INTO people (restaurant_id, display_name, name_key, created_via) VALUES "
                            "(?,?,?,?)", (rid, "Antonio Corona Martinez", "antonio corona martinez", "rpower")).lastrowid
        ghost = conn.execute("INSERT INTO people (restaurant_id, display_name, name_key, created_via) VALUES "
                             "(?,?,?,?)", (rid, "Antonio Corona Martinez · overall", "antonio corona martinez · overall",
                                           "store:capability_changes")).lastrowid
        conn.execute("INSERT INTO person_questions (restaurant_id, person_a, person_b, kind, reason) VALUES "
                     "(?,?,?,?,?)", (rid, ghost, real, "same_person", "a close spelling"))
        conn.execute("INSERT INTO capability_changes (restaurant_id, kind, subject, person_id) VALUES (?,?,?,?)",
                     (rid, "rating", "Antonio Corona Martinez · overall", ghost))
        conn.commit()
    finally:
        conn.close()
    out = people.repair_labelled_people(db_path=db_path)
    assert out["people"] == 1 and out["questions"] == 1
    assert [p["display_name"] for p in _rows(db_path, "SELECT display_name FROM people WHERE restaurant_id=?",
                                             (rid,))] == ["Antonio Corona Martinez"]
    assert _rows(db_path, "SELECT * FROM person_questions WHERE restaurant_id=?", (rid,)) == []
    row = _rows(db_path, "SELECT subject, attribute, person_id FROM capability_changes WHERE restaurant_id=?", (rid,))[0]
    assert row == {"subject": "Antonio Corona Martinez", "attribute": "overall", "person_id": real}
    assert people.repair_labelled_people(db_path=db_path)["people"] == 0
