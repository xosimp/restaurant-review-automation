"""Memory fix round 9/29/26, workstream M8 — the cross-restaurant learning
table (intelligence.feedback / scoring):

  per_episode    one row per EPISODE's answer, each with its own time, so
                 nine declines and one take are ten recommendations and a
                 recent answer is inside the prior's window (PLATFORM-4);
                 bare-key rows are re-derived once from rec_events.
  stale_cohorts  every sync re-stamps a restaurant's rows whenever its
                 labels change — the cohort set to today's value, NULL
                 included, for seeded and excluded restaurants too; a
                 deleted restaurant's kept rows keep theirs (PLATFORM-15).
  sync_bounded   trackers carry a change stamp; a night re-reads only the
                 changed ones (and their number's group), and the
                 confidence fill only young rows (PLATFORM-18).
  org_map        priors count organisations with privacy.org_map — a
                 shared owner login joins two emails (PLATFORM-19).
"""
import json
import sys
from datetime import datetime, timedelta

import pytest

import models
import rec_ledger as rl
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import intelligence  # noqa: E402
from intelligence import feedback, scoring, jobs, privacy  # noqa: E402


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    auth.init_auth(db_path=db_path)
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()
    yield db_path
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()


def _rid(db, name="Episode Co", email=None, **kw):
    return create_restaurant(Restaurant(name=name, owner_email=email or f"{name.split()[0].lower()}@x.test",
                                        module_labor=1, **kw), db_path=db)


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _episode(db, rid, key, answer, days_ago=0, meta=None):
    """A shown episode answered `answer`, dated `days_ago`."""
    rec = rl.present(rid, key, "labor", "home", db_path=db)
    rl.record(rid, key, answer, surface="home", meta=meta, db_path=db)
    at = f"-{int(days_ago)} days"
    _x(db, "UPDATE rec_instances SET created_at=datetime('now', ?), last_event_at=datetime('now', ?), "
           "closed_at=datetime('now', ?), silenced_until=NULL WHERE rec_id=?", (at, at, at, rec))
    _x(db, "UPDATE rec_events SET at=datetime('now', ?) WHERE rec_id=?", (at, rec))
    return rec


# ══ per_episode ══════════════════════════════════════════════════════════════

def test_nine_declines_and_one_take_are_ten_recommendations_not_one_taken(db):
    rid = _rid(db)
    for i in range(9):
        _episode(db, rid, "trim_day:Monday", "dismissed", days_ago=100 - i, meta={"kind": "not_for_us"})
    _episode(db, rid, "trim_day:Monday", "accepted", days_ago=5)
    feedback.sync(db_path=db)
    rows = _q(db, "SELECT source_key, action, rec_id, event_at FROM intel_rec_events WHERE restaurant_id=?", (rid,))
    assert len(rows) == 10 and len({r["source_key"] for r in rows}) == 10
    assert all(feedback.base_key(r["source_key"]) == "trim_day:Monday" and r["rec_id"] for r in rows)
    assert {r["source_key"] for r in rows} == {feedback.episode_key("trim_day:Monday", r["rec_id"]) for r in rows}
    s = scoring.kind_stats("trim_day", restaurant_id=rid, db_path=db)
    assert (s["accepted"], s["declined"]) == (1, 9) and s["acceptance_rate"] == 0.1
    # each episode keeps its own time: the latest answer is days old, not the first one's
    assert max(r["event_at"] for r in rows) > (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")


def test_a_key_hidden_long_ago_and_again_last_week_stays_in_the_window(db):
    rid = _rid(db)
    _episode(db, rid, "cut_waste:Salmon", "dismissed", days_ago=800, meta={"kind": "hide"})
    _episode(db, rid, "cut_waste:Salmon", "dismissed", days_ago=7, meta={"kind": "hide"})
    feedback.sync(db_path=db)
    s = scoring.kind_stats("cut_waste", restaurant_id=rid, db_path=db, window_days=365)
    assert s["hidden"] == 1               # the recent hide counts; it used to vanish with the first


def test_the_per_episode_repair_re_derives_bare_ledger_rows_once(db):
    rid = _rid(db)
    _episode(db, rid, "trim_day:Friday", "completed", days_ago=3)
    # what the old sync wrote: one bare-key row for the key
    _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, synced_from) "
           "VALUES (?, 'trim_day', 'trim_day:Friday', 'done', datetime('now','-3 days'), 'rec_ledger')", (rid,))
    # ... and a bare row the ledger no longer holds (pruned): kept
    _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, synced_from) "
           "VALUES (?, 'trim_day', 'trim_day:Sunday', 'done', datetime('now','-900 days'), 'rec_ledger')", (rid,))
    _x(db, "DELETE FROM job_cursors WHERE key=?", (feedback.PER_EPISODE_REPAIR_MARK,))
    _x(db, "INSERT INTO job_cursors (key, value) VALUES (?, '99999')", (feedback.LEDGER_CURSOR,))
    feedback.sync(db_path=db)
    keys = {r["source_key"] for r in _q(db, "SELECT source_key FROM intel_rec_events WHERE restaurant_id=?", (rid,))}
    assert "trim_day:Friday" not in keys and "trim_day:Sunday" in keys
    assert any(k.startswith("trim_day:Friday#e") for k in keys)
    assert _q(db, "SELECT value FROM job_cursors WHERE key=?", (feedback.PER_EPISODE_REPAIR_MARK,))[0]["value"] == "1"
    n = len(_q(db, "SELECT 1 FROM intel_rec_events"))
    feedback.sync(db_path=db)                                 # once: nothing more
    assert len(_q(db, "SELECT 1 FROM intel_rec_events")) == n


def test_a_home_answer_and_its_ledger_copy_are_one_row(db):
    import home_brief
    rid = _rid(db)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db)
    home_brief.dismiss(rid, "trim_day:Tuesday", kind="not_for_us")
    feedback.sync(db_path=db)
    rows = _q(db, "SELECT source_key, action FROM intel_rec_events WHERE restaurant_id=?", (rid,))
    assert [(feedback.base_key(r["source_key"]), r["action"]) for r in rows] == [("trim_day:Tuesday", "not_for_us")]
    assert rows[0]["source_key"].startswith("trim_day:Tuesday#e")


def test_the_row_suffix_is_read_only_in_its_own_shape():
    assert feedback.base_key("reprice:Burger #one") == "reprice:Burger #one"
    assert feedback.base_key("trim_day:Monday#o12") == "trim_day:Monday"
    assert feedback.base_key(feedback.episode_key("schedule_to_target", "a" * 32)) == "schedule_to_target"
    assert feedback.kind_of(feedback.episode_key("schedule_to_target", "b" * 32)) == "schedule_to_target"


# ══ stale_cohorts ════════════════════════════════════════════════════════════

def test_a_guessed_cohort_is_cleared_on_the_next_sync_seeded_and_excluded_restaurants_too(db):
    guessed = _rid(db, "Tony's Pizzeria")                      # a type Cavnar only guesses
    demo = _rid(db, "Demo Pizzeria", is_demo=1)
    confirmed = _rid(db, "Set Co")
    models.update_restaurant(confirmed, {"service_model": "counter", "concept": "pizza", "category": "pizza",
                                         "profile_source": "set", "profile_confirmed_at": "2026-09-01T00:00:00"})
    for r in (guessed, demo, confirmed):
        _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, event_at) "
               "VALUES (?, 'trim_day', 'trim_day:Monday', 'pizza', 'done', datetime('now'))", (r,))
    # a deleted restaurant's kept, anonymised rows (no restaurants row —
    # models.delete_restaurant runs with foreign keys off, as here)
    c = models.get_conn(db)
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, event_at) "
              "VALUES (987654, 'trim_day', 'trim_day:Monday', 'pizza', 'done', datetime('now'))")
    c.commit()
    c.close()
    jobs.run_learning(db_path=db)
    by = {r["restaurant_id"]: r for r in _q(db, "SELECT restaurant_id, cohort, partition_key FROM intel_rec_events")}
    assert by[guessed]["cohort"] is None and by[demo]["cohort"] is None
    assert by[confirmed]["cohort"] == "pizza" and by[confirmed]["partition_key"] == "sm:counter"
    assert by[987654]["cohort"] == "pizza"                      # kept, and still counts toward the group
    # a later confirmation re-stamps that restaurant's rows (its labels changed)
    models.update_restaurant(guessed, {"service_model": "full_service", "concept": "italian",
                                       "profile_source": "set", "profile_confirmed_at": "2026-09-02T00:00:00"})
    feedback.sync(db_path=db)
    row = _q(db, "SELECT cohort, partition_key FROM intel_rec_events WHERE restaurant_id=?", (guessed,))[0]
    assert row == {"cohort": "italian", "partition_key": "sm:full_service"}


def test_a_restaurant_whose_labels_did_not_change_is_not_re_read(db, monkeypatch):
    rid = _rid(db)
    _episode(db, rid, "trim_day:Monday", "accepted", days_ago=2)
    feedback.sync(db_path=db)
    seen = []
    real = feedback._stamp_labels

    def spy(conn, labels, provenance):
        seen.append(set(labels))
        return real(conn, labels, provenance)
    monkeypatch.setattr(feedback, "_stamp_labels", spy)
    feedback.sync(db_path=db)
    mark = json.loads(_q(db, "SELECT value FROM job_cursors WHERE key=?", (feedback.LABELS_MARK,))[0]["value"])
    assert str(rid) in mark and seen                            # stamped once, remembered


# ══ sync_bounded ═════════════════════════════════════════════════════════════

def _tracker(db, rid, key, verdict="improved", days_ago=40, metric="labor_pct"):
    c = models.get_conn(db)
    try:
        cur = c.execute(
            "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
            "evaluate_on, status, verdict, created_at) VALUES (?, 'recommendation', ?, 't', ?, date('now', ?), "
            "date('now', ?), 'evaluated', ?, datetime('now', ?))",
            (rid, key, metric, f"-{days_ago} days", f"-{days_ago - 28} days", verdict, f"-{days_ago} days"))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def test_a_tracker_carries_a_change_stamp_the_accrual_does_not_move(db):
    rid = _rid(db)
    tid = _tracker(db, rid, "trim_day:Monday")
    first = _q(db, "SELECT changed_at FROM recommendation_outcomes WHERE id=?", (tid,))[0]["changed_at"]
    assert first
    _x(db, "UPDATE recommendation_outcomes SET accrued_through=date('now') WHERE id=?", (tid,))
    assert _q(db, "SELECT changed_at FROM recommendation_outcomes WHERE id=?", (tid,))[0]["changed_at"] == first
    _x(db, "UPDATE recommendation_outcomes SET recheck_verdict='faded' WHERE id=?", (tid,))
    assert _q(db, "SELECT changed_at FROM recommendation_outcomes WHERE id=?", (tid,))[0]["changed_at"] > first


def test_the_sync_re_reads_only_changed_trackers_and_their_numbers_group(db):
    rid, other = _rid(db), _rid(db, "Other Co")
    a = _tracker(db, rid, "trim_day:Monday", days_ago=200)
    _tracker(db, rid, "trim_day:Tuesday", days_ago=120)
    _tracker(db, rid, "cut_waste:Salmon", metric="weekly_waste", days_ago=90)
    _tracker(db, other, "trim_day:Friday", days_ago=60)
    first = feedback.sync(db_path=db)
    assert first["trackers_read"] == 4
    assert feedback.sync(db_path=db)["trackers_read"] == 0          # nothing changed: nothing re-read
    _x(db, "UPDATE recommendation_outcomes SET verdict='worsened' WHERE id=?", (a,))
    again = feedback.sync(db_path=db)
    assert again["trackers_read"] == 2                          # the changed one and its number's other result
    out = _q(db, "SELECT outcome FROM intel_rec_events WHERE source_key=?", (feedback.measured_key("trim_day:Monday", a),))
    assert out[0]["outcome"] == "worsened"


def test_the_confidence_fill_reads_only_young_rows(db):
    rid = _rid(db)
    _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, created_at) "
           "VALUES (?, 'trim_day', 'trim_day:Old', 'done', datetime('now','-90 days'), datetime('now','-90 days'))",
       (rid,))
    _x(db, "INSERT INTO rec_instances (rec_id, restaurant_id, key, kind, status, confidence_pct, trust_version, "
           "created_at) VALUES ('x1', ?, 'trim_day:Old', 'trim_day', 'completed', 70, 2, datetime('now','-100 days'))",
       (rid,))
    feedback.sync(db_path=db)
    row = _q(db, "SELECT confidence_at FROM intel_rec_events WHERE source_key='trim_day:Old'")[0]
    assert row["confidence_at"] is None                         # derived 90 days ago: never re-scanned
    _x(db, "UPDATE intel_rec_events SET created_at=datetime('now') WHERE source_key='trim_day:Old'")
    feedback.sync(db_path=db)
    row = _q(db, "SELECT confidence_at, trust_version FROM intel_rec_events WHERE source_key='trim_day:Old'")[0]
    assert row == {"confidence_at": 0.7, "trust_version": 2}


# ══ org_map ══════════════════════════════════════════════════════════════════

def test_two_emails_behind_one_owner_login_are_one_organisation_in_a_prior(db):
    import auth
    rids = [_rid(db, f"Solo {i}", email=f"solo{i}@x.test") for i in range(4)]
    twin_a = _rid(db, "Twin A", email="twin.a@x.test")
    twin_b = _rid(db, "Twin B", email="twin.b@x.test")
    uid = auth.create_user(twin_a, "twin_owner", "twin@x.test", "pw", role="client", db_path=db)
    _x(db, "INSERT INTO memberships (user_id, restaurant_id, role, is_active) VALUES (?, ?, 'owner', 1)",
       (uid, twin_b))
    scoring.invalidate_org_map()
    for r in rids + [twin_a, twin_b]:
        _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at) "
               "VALUES (?, 'trim_day', ?, 'done', datetime('now'))", (r, f"trim_day:{r}"))
    orgs = scoring.org_map([twin_a, twin_b] + rids, db_path=db)
    assert orgs[twin_a] == orgs[twin_b] and len(set(orgs.values())) == 5
    assert privacy.org_map(db_path=db)[twin_a] == orgs[twin_a]      # the bands' organisation
    s = scoring.kind_stats("trim_day", db_path=db)
    assert s["answered_restaurants"] == 6 and s["answered_orgs"] == 5
    # The asking restaurant's whole organisation is out of its prior: the twin too.
    mine = scoring.kind_stats("trim_day", db_path=db, exclude_restaurant_id=twin_a)
    assert mine["answered_restaurants"] == 4 and not mine["acceptance_available"]
