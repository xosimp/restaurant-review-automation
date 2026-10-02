"""A review answered outside Cavnar AI (owner, 10/2/26: Danny replied to the
urgent reviews on Google by hand and the app still told Erik to reply).

Marked answered - posted, out of the reply queue, the urgent count and the
reminders - by the Replied-on-Google button or by a Business Profile fetch
that sees Google's own reply. Never Cavnar AI's draft going out: it teaches
the drafter nothing, counts as no work Cavnar AI did, and Retract never
deletes it."""
import inspect
import sqlite3
import sys

import pytest
from flask import Flask

import models
from models import Restaurant, Review, create_restaurant, save_reviews


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.test"), db_path=db_path)


def _review(rid, db_path, name, rating=2, status="drafted", draft="Thanks for telling us."):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=name, author="Ann", rating=rating,
                         text="Slow service", review_date="2026-09-29T18:32:50", review_name=name)],
                 db_path=db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE reviews SET response_status=?, draft_response=?, processed=1 WHERE review_name=?",
              (status, draft, name))
    c.commit()
    rid_ = c.execute("SELECT id FROM reviews WHERE review_name=?", (name,)).fetchone()[0]
    c.close()
    return rid_


def _row(db_path, review_id):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    r = dict(c.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone())
    c.close()
    return r


def test_a_waiting_review_is_marked_answered_and_undone(rid, db_path):
    rv = _review(rid, db_path, "accounts/1/locations/2/reviews/a")
    assert models.mark_replied_elsewhere(rv, rid, source="owner", db_path=db_path)
    r = _row(db_path, rv)
    assert (r["response_status"], r["response_action"], r["external_reply_source"]) == \
        ("posted", "replied_elsewhere", "owner")
    assert r["posted_at"] and r["approved_at"] is None
    # Out of the urgent count and the queue.
    stats = models.get_review_stats(rid)
    assert stats.get("urgent", 0) == 0
    assert models.undo_replied_elsewhere(rv, rid, db_path=db_path)
    r = _row(db_path, rv)
    assert (r["response_status"], r["response_action"], r["external_reply"]) == ("drafted", None, None)


def test_an_approved_or_posted_reply_of_ours_is_never_relabelled(rid, db_path):
    a = _review(rid, db_path, "n/approved", status="approved")
    p = _review(rid, db_path, "n/posted", status="posted")
    assert not models.mark_replied_elsewhere(a, rid, db_path=db_path)
    assert not models.mark_replied_elsewhere(p, rid, db_path=db_path)
    assert not models.undo_replied_elsewhere(p, rid, db_path=db_path), "undo only takes off its own mark"


def test_google_replies_mark_waiting_reviews_and_land_approved_ones(rid, db_path):
    w = _review(rid, db_path, "n/waiting")
    a = _review(rid, db_path, "n/approved", status="approved")
    s = _review(rid, db_path, "n/skipped", status="skipped")
    untouched = _review(rid, db_path, "n/no-reply")
    n = models.apply_google_replies(rid, {
        "n/waiting": {"comment": "So sorry, Ann - we've talked to the team.", "update_time": "2026-10-02T14:05:00Z"},
        "n/approved": {"comment": "Our approved reply", "update_time": "2026-10-02T15:00:00.123Z"},
        "n/skipped": {"comment": "Thanks!", "update_time": ""},
        "n/elsewhere-unknown": {"comment": "x", "update_time": ""},
    }, db_path=db_path)
    assert n == 3
    r = _row(db_path, w)
    assert (r["response_status"], r["response_action"], r["external_reply_source"]) == \
        ("posted", "replied_elsewhere", "google")
    assert r["external_reply"].startswith("So sorry") and r["posted_at"] == "2026-10-02 14:05:00"
    r = _row(db_path, a)
    assert (r["response_status"], r["response_action"]) == ("posted", None), "our approved reply is now live"
    assert _row(db_path, s)["response_action"] == "replied_elsewhere"
    assert _row(db_path, untouched)["response_status"] == "drafted"


def test_an_answer_elsewhere_teaches_the_drafter_nothing(rid, db_path):
    rv = _review(rid, db_path, "n/voice", rating=5, draft="Thank you so much!")
    models.mark_replied_elsewhere(rv, rid, db_path=db_path)
    assert models.get_approved_examples(rid, db_path=db_path) == []
    src = inspect.getsource(models)
    assert src.count("'support_approved', 'replied_elsewhere')") == 4, "every draft-learning read leaves it out"
    import admin_ops
    assert "'support_approved','replied_elsewhere')" in inspect.getsource(admin_ops)


def test_it_is_not_counted_as_work_cavnar_ai_did(rid, db_path):
    import value_delivered
    rv = _review(rid, db_path, "n/value")
    models.mark_replied_elsewhere(rv, rid, db_path=db_path)
    assert value_delivered.ledger(rid, db_path=db_path)["replies_posted"] == 0


def test_the_routes_and_retract_never_deletes_their_reply(rid, db_path, monkeypatch):
    import client_api
    import rec_ledger
    monkeypatch.setattr(rec_ledger, "implemented", lambda *a, **k: None)
    rv = _review(rid, db_path, "n/route")
    u = {"restaurant_id": rid, "id": 1, "role": "manager"}
    app = Flask(__name__)
    with app.test_request_context("/x", method="POST"):
        out, code = client_api._do_replied_elsewhere(rv, u)
        assert code == 200 and out["response_action"] == "replied_elsewhere"
        assert client_api._do_replied_elsewhere(rv, u)[0].get("already")
        assert client_api._do_retract(rv, rid)[1] == 409
        assert client_api._do_undo(rv, rid)[1] == 200
        assert _row(db_path, rv)["response_status"] == "drafted"
        assert client_api._do_replied_elsewhere(999999, u)[1] == 404
    a = _review(rid, db_path, "n/route-approved", status="approved")
    with app.test_request_context("/x", method="POST"):
        assert client_api._do_replied_elsewhere(a, u)[1] == 409
    # Web and phone twins over one body.
    import mobile_api
    assert "_capi._do_replied_elsewhere(review_id, current_user)" in inspect.getsource(mobile_api)
    assert '@client_bp.route("/api/reviews/<int:rid>/replied-elsewhere", methods=["POST"])' in \
        inspect.getsource(client_api)


def test_the_card_shows_their_reply_not_our_draft(rid, db_path):
    rv = _review(rid, db_path, "n/card", draft="OUR UNUSED DRAFT")
    models.apply_google_replies(rid, {"n/card": {"comment": "Danny's reply", "update_time": "2026-10-02T14:05:00Z"}},
                                db_path=db_path)
    row = [r for r in models.get_reviews_data(rid) if r["id"] == rv][0]
    assert row["replied_elsewhere"] and not row["can_retract"]
    tpl = open("templates/_review_card.html", encoding="utf-8").read()
    assert "{% if r.replied_elsewhere %}" in tpl and "undoElsewhereR({{ r.id }},this)" in tpl
    assert tpl.count("repliedElsewhereR({{ r.id }},this)") == 3


def test_the_business_profile_fetch_reads_googles_reply():
    import gmb
    import scheduler
    src = inspect.getsource(gmb.fetch_reviews_via_gmb)
    assert 'r.get("reviewReply")' in src and "out.replies = replies" in src
    assert '"orderBy": "updateTime desc"' in src
    assert "apply_google_replies(rid, gbp_listing[1].replies)" in inspect.getsource(scheduler.run_daily_fetch)
