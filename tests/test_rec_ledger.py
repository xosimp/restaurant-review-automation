"""rec_ledger: one identity and trail for every recommendation."""
import rec_ledger as rl
from models import Restaurant, create_restaurant, get_conn


def _rid(db_path):
    return create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)


def test_one_recommendation_shown_on_two_surfaces_is_one_episode(db_path):
    rid = _rid(db_path)
    a = rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db_path)
    b = rl.present(rid, "trim_day:Monday", "labor", "brief_email", db_path=db_path)
    assert a and a == b
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db_path)       # same surface, same day
    c = get_conn(db_path)
    shown = c.execute("SELECT COUNT(*) FROM rec_events WHERE rec_id=? AND event='shown'", (a,)).fetchone()[0]
    c.close()
    assert shown == 2


def test_no_in_one_place_is_no_everywhere(db_path):
    rid = _rid(db_path)
    rl.present(rid, "cut_waste:Salmon", "food", "home", db_path=db_path)
    rl.record(rid, "cut_waste:Salmon", "dismissed", surface="brief_email", meta={"kind": "not_for_us"}, db_path=db_path)
    assert rl.silenced(rid, "cut_waste:Salmon", db_path=db_path)
    assert rl.present(rid, "cut_waste:Salmon", "food", "alert_push", db_path=db_path) is None
    rl.unsilence(rid, "cut_waste:Salmon", db_path=db_path)
    assert rl.present(rid, "cut_waste:Salmon", "food", "home", db_path=db_path)


def test_an_unanswered_recommendation_expires_as_ignored(db_path):
    rid = _rid(db_path)
    rec = rl.present(rid, "post_this_week", "marketing", "home", db_path=db_path)
    c = get_conn(db_path)
    # Expiry runs from when the episode was created (reaudit H-2).
    c.execute("UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE rec_id=?", (rec,))
    c.commit(); c.close()
    assert rl.expire_stale(db_path=db_path) == 1
    again = rl.present(rid, "post_this_week", "marketing", "home", db_path=db_path)
    assert again and again != rec


def test_a_snooze_hides_until_it_ends(db_path):
    rid = _rid(db_path)
    rl.present(rid, "reprice:Carbonara", "food", "queue", db_path=db_path)
    rl.record(rid, "reprice:Carbonara", "snoozed", snooze_until="2999-01-01 00:00:00", db_path=db_path)
    assert rl.silenced(rid, "reprice:Carbonara", db_path=db_path)


def test_a_repeated_answer_is_recorded_once(db_path):
    rid = _rid(db_path)
    assert rl.record(rid, "publish_drafts", "completed", source_ref="x1", db_path=db_path)
    assert not rl.record(rid, "publish_drafts", "completed", source_ref="x1", db_path=db_path)
