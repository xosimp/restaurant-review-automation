"""Who takes covers, promotions and guests naming staff are captured
(memory audit 9/29/26, uncaptured — M3's part: the person_signals ledger and
person_roles; review re-tags and request conversion are M6's).

Cover suggestions ranked by rating then alphabetically, ignoring who said
yes last time; a server trained on bar was never offered a bartender gap
until she had worked bar shifts; "Maria was amazing" never reached Maria.
"""
from datetime import date, timedelta

import pytest

import labor_replacements
import models
import people
import shift_facts
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Signals Co", owner_email="s@x.test", module_labor=1))


def _history(rid, people_roles, day=date(2026, 9, 1)):
    rows = [{"date": day.isoformat(), "day": day.strftime("%A"), "employee": n, "role": r, "shift_start": "16:00",
             "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "6", "sales": "1000"}
            for n, r in people_roles]
    shift_facts.ingest(rid, rows, "upload")


# ── promotions and trained roles ────────────────────────────────────────────

def test_a_server_trained_on_bar_is_offered_for_a_bartender_gap():
    rid = _rid()
    _history(rid, [("Ana B.", "Server"), ("Ben C.", "Bartender"), ("Cy D.", "Server")])
    assert "Ana B." not in {f["name"] for f in labor_replacements.for_gap(rid, "Bartender", "Friday", limit=5)}
    people.add_role(rid, "Ana B.", "Bartender", since="9/1/26")
    assert "bartender" in staff_settings.roles_for(rid, "Ana B.")
    assert "Ana B." in {f["name"] for f in labor_replacements.for_gap(rid, "Bartender", "Friday", limit=5)}
    assert "Cy D." not in {f["name"] for f in labor_replacements.for_gap(rid, "Bartender", "Friday", limit=5)}


def test_a_promotion_is_the_persons_role_on_the_roster_from_its_date():
    rid = _rid()
    _history(rid, [("Ana B.", "Server")])
    people.add_role(rid, "Ana B.", "Bartender", since=(date.today() + timedelta(days=7)).isoformat(), primary=True)
    assert next(e for e in staff_settings.roster(rid) if e["name"] == "Ana B.")["role"] == "Server"   # not yet
    people.add_role(rid, "Ana B.", "Bartender", since=date.today().isoformat(), primary=True)
    assert next(e for e in staff_settings.roster(rid) if e["name"] == "Ana B.")["role"] == "Bartender"
    assert people.remove_role(rid, "Ana B.", "Bartender") is True
    assert next(e for e in staff_settings.roster(rid) if e["name"] == "Ana B.")["role"] == "Server"


def test_re_adding_a_held_role_keeps_it_the_main_role():
    """Adding a role again (a new start date, say) without ticking "A
    promotion" must not quietly demote a promotion (parity round 10/7/26)."""
    rid = _rid()
    _history(rid, [("Ana B.", "Server")])
    people.add_role(rid, "Ana B.", "Bartender", since=date.today().isoformat(), primary=True)
    people.add_role(rid, "Ana B.", "Bartender", since=(date.today() - timedelta(days=30)).isoformat())
    assert next(e for e in staff_settings.roster(rid) if e["name"] == "Ana B.")["role"] == "Bartender"


# ── who takes covers ────────────────────────────────────────────────────────

def test_whoever_took_a_cover_when_asked_is_suggested_first():
    rid = _rid()
    _history(rid, [("Ana B.", "Server"), ("Ben C.", "Server"), ("Zed Q.", "Server")])
    conn = models.get_conn()
    conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, meta_json) VALUES "
                 "(?,?,?,?,?,?)", (rid, "coverage", "coverage:2026-09-20:tom b.", "x", "resolved",
                                   '{"missing": "Tom B.", "asked": [{"name": "Zed Q."}, {"name": "Ben C."}]}'))
    conn.commit()
    conn.close()
    _history(rid, [("Zed Q.", "Server")], day=date(2026, 9, 20))          # Zed came in that day
    # Zed took it; Ben was asked too but the shift was already covered —
    # not needed, never "declined" (schedule re-audit 10/4/26 LEARN-8).
    assert people.record_cover_signals(rid, today=date(2026, 9, 29), days=30) == 1
    rec = people.cover_record(rid, days=3650)
    assert rec[staff_settings.name_key("Zed Q.")]["accepted"] == 1
    assert staff_settings.name_key("Ben C.") not in rec
    fits = labor_replacements.for_gap(rid, "Server", "Friday", limit=3)
    assert [f["name"] for f in fits][0] == "Zed Q."                        # not alphabetical any more
    assert fits[0]["covers_taken"] == 1


def test_the_managers_word_on_a_cover_stands_over_the_punches():
    rid = _rid()
    _history(rid, [("Ana B.", "Server"), ("Zed Q.", "Server")])
    conn = models.get_conn()
    iid = conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, meta_json) VALUES "
                       "(?,?,?,?,?,?)", (rid, "coverage", "coverage:2026-09-20:tom b.", "x", "resolved",
                                         '{"missing": "Tom B.", "asked": [{"name": "Zed Q."}]}')).lastrowid
    conn.commit()
    conn.close()
    # No punch from Zed that day: the job infers he said no.
    assert people.record_cover_signals(rid, today=date(2026, 9, 29), days=30) == 1
    assert people.cover_record(rid, days=3650)[staff_settings.name_key("Zed Q.")] == {"accepted": 0, "declined": 1}
    # The manager says he came in (the POS put his punch on another job code).
    people.answer_cover(rid, "Zed Q.", iid, True, "2026-09-20", user={"id": 5, "role": "manager"})
    assert people.cover_record(rid, days=3650)[staff_settings.name_key("Zed Q.")] == {"accepted": 1, "declined": 0}
    people.record_cover_signals(rid, today=date(2026, 9, 29), days=30)      # the job runs again
    assert people.cover_record(rid, days=3650)[staff_settings.name_key("Zed Q.")] == {"accepted": 1, "declined": 0}
    conn = models.get_conn()
    row = conn.execute("SELECT created_by, authority FROM person_signals WHERE restaurant_id=? AND "
                       "kind='cover_accepted'", (rid,)).fetchone()
    conn.close()
    assert (row["created_by"], row["authority"]) == (5, "delegate")


# ── guests naming staff ─────────────────────────────────────────────────────

def _review(rid, text, sentiment="positive"):
    conn = models.get_conn()
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                 "fetched_at, processed, sentiment) VALUES (?,?,?,?,?,?,date('now','-2 days'),datetime('now'),1,?)",
                 (rid, "google", f"r{abs(hash(text)) % 100000}", "Guest", 5 if sentiment == "positive" else 1, text,
                  sentiment))
    conn.commit()
    conn.close()


def test_a_guest_naming_staff_is_proposed_and_counts_only_once_confirmed():
    rid = _rid()
    _history(rid, [("Maria Garcia", "Server"), ("Will Stone", "Bartender")])
    _review(rid, "Maria was amazing, we will be back!")
    _review(rid, "Will S. made the best old fashioned.")
    _review(rid, "Will definitely return.")
    n = people.match_review_mentions(rid)
    got = {(m["name"], m["polarity"]) for m in people.mentions(rid)}
    assert got == {("Maria Garcia", 1), ("Will Stone", 1)} and n == 2
    maria = next(m for m in people.mentions(rid) if m["name"] == "Maria Garcia")
    assert people.get_person(rid, "maria-garcia")["guest_mentions"] == []   # not until confirmed
    assert people.answer_mention(rid, maria["id"], True, user={"id": 3, "role": "client"}) is True
    assert people.get_person(rid, "maria-garcia")["guest_mentions"][0]["polarity"] == 1
    conn = models.get_conn()
    assert conn.execute("SELECT authority FROM person_signals WHERE id=?", (maria["id"],)).fetchone()[0] == "principal"
    conn.close()


def test_two_people_with_one_first_name_are_never_guessed():
    rid = _rid()
    _history(rid, [("Maria Garcia", "Server"), ("Maria Lopez", "Server")])
    _review(rid, "Maria was wonderful.")
    assert people.match_review_mentions(rid) == 0


def test_the_person_record_says_attendance_is_unknown_when_nobody_watched():
    rid = _rid()
    _history(rid, [("Ana B.", "Server")])
    p = people.get_person(rid, "ana-b")
    assert p["attendance"] == {"known": False}
    assert p["roles_held"] == [] and p["covers"] == {"taken": 0, "declined": 0, "days": 180}
