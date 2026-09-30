"""Memory re-audit fix round 9/29/26 (R5, holdout — LOOPS-3, and LOOPS-15's
reply outcomes): a deterministic holdout per (restaurant, day) for Home and
the one thing, per review for the reply style note and per day for order
corrections; arms logged and compared in the admin readouts only."""
import json
import sys

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Arm Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def test_the_arm_is_stable_bounded_and_off_in_the_suite(monkeypatch):
    import rec_learning
    assert rec_learning.holdout_pct() == 0                       # conftest: off by default in tests
    assert rec_learning.holdout_arm(1, "2026-10-01", "home") == "learned"
    monkeypatch.setenv("LEARNING_HOLDOUT_PCT", "10")
    arms = [rec_learning.holdout_arm(7, f"2026-10-{d:02d}", "home") for d in range(1, 31)]
    arms += [rec_learning.holdout_arm(7, f"2026-11-{d:02d}", "home") for d in range(1, 31)]
    assert set(arms) <= {"learned", "holdout"}
    assert arms == [rec_learning.holdout_arm(7, f"2026-10-{d:02d}", "home") for d in range(1, 31)] + \
        [rec_learning.holdout_arm(7, f"2026-11-{d:02d}", "home") for d in range(1, 31)]
    share = arms.count("holdout") / len(arms)
    assert 0.0 < share < 0.3
    monkeypatch.setenv("LEARNING_HOLDOUT_PCT", "95")
    assert rec_learning.holdout_pct() == rec_learning.HOLDOUT_MAX_PCT


def test_a_held_out_build_ranks_on_neutral_weights_and_logs_its_arm(monkeypatch):
    import rec_learning
    import rec_ledger
    rid = _rid()
    learned = rec_learning.effectiveness(rid)
    monkeypatch.setattr(rec_learning, "holdout_arm", lambda *a, **k: "holdout")
    rec_learning.apply_holdout(learned, rid, "home")
    assert learned.arm == "holdout"
    assert learned.weight("trim_day:Friday") == (1.0, [])
    info = rec_learning.weigh(learned, "trim_day:Friday")
    assert info["weight"] == 1.0 and info["arm"] == "holdout"
    meta = rec_learning.rank_meta({"rank_score": 5}, 5, info)
    assert meta["arm"] == "holdout"
    rec_ledger.present_many(rid, [{"key": "trim_day:Friday", "module": "labor", "rank": meta}], "home")
    assert rec_ledger.log_rank_build(rid, "home", shown=[dict(meta, key="trim_day:Friday")], arm="holdout")
    conn = models.get_conn()
    try:
        ev = conn.execute("SELECT meta FROM rec_events WHERE restaurant_id=? AND event='shown'", (rid,)).fetchone()
        row = conn.execute("SELECT arm FROM rec_rank_builds WHERE restaurant_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert json.loads(ev["meta"])["rank"]["arm"] == "holdout" and row["arm"] == "holdout"


def test_rank_learning_compares_the_arms(monkeypatch):
    import admin_ops
    import rec_learning
    import rec_ledger
    monkeypatch.setattr(admin_ops, "get_conn", models.get_conn, raising=False)
    rid = _rid()
    for i, arm in enumerate(["learned"] * 3 + ["holdout"] * 2):
        key = f"trim_day:D{i}"
        rec_ledger.present_many(rid, [{"key": key, "module": "labor",
                                       "rank": {"weight": 1.0, "version": 3, "arm": arm}}], "home")
        if i % 2 == 0:
            rec_ledger.record(rid, key, "accepted", surface="home", authority="principal")
    out = admin_ops.rank_learning(days=30, restaurant_id=rid, include_internal=True)
    arms = {a["arm"]: a for a in out["arms"]}
    assert arms["learned"]["shown"] == 3 and arms["holdout"]["shown"] == 2
    assert "holdouts" in out and "reply_note" in out["holdouts"]
    assert rec_learning.ARMS == ("learned", "holdout")


def test_a_held_out_reply_draft_gets_no_style_note(monkeypatch):
    import drafter
    rid = _rid()
    conn = models.get_conn()
    for i in range(3):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at, "
                     "draft_response, original_draft, response_status, edit_category, edit_signals, approved_at) "
                     "VALUES (?,'manual',?,?,?,?,?,?,?,?,?,?,datetime('now','-5 days'))",
                     (rid, f"e{i}", "g", 5, "great", "2026-09-01", "Thanks.", "Thanks!", "approved", "light",
                      json.dumps(["removed_exclamations"])))
    conn.commit()
    conn.close()
    assert "exclamation" in drafter.get_owner_edit_note(rid, rating=5, review_id=123)
    monkeypatch.setattr(drafter, "reply_note_arm", lambda *a, **k: "holdout")
    assert "exclamation" not in drafter.get_owner_edit_note(rid, rating=5, review_id=123)
    import inspect
    assert "review_id=review_id" in inspect.getsource(drafter.draft_response)


def test_a_held_out_order_day_marks_the_lines_it_left_alone():
    import inspect
    import inventory
    src = inspect.getsource(inventory)
    assert '"order_corrections") == "holdout"' in src and 'line["correction_held_out"]' in src


def test_the_holdout_readout_reads_orders_replies_and_guest_edits(monkeypatch):
    import admin_ops
    monkeypatch.setattr(admin_ops, "get_conn", models.get_conn, raising=False)
    rid = _rid()
    conn = models.get_conn()
    draft = [{"ingredient_id": 1, "qty": 10, "owner_adjusted": {"factor": 0.8}},
             {"ingredient_id": 2, "qty": 10, "correction_held_out": 0.8}]
    sent = [{"ingredient_id": 1, "qty": 10}, {"ingredient_id": 2, "qty": 8}]
    conn.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_email, items_json, "
                 "draft_items_json, source, sent_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                 (rid, "PO-1", "s@x.test", json.dumps(sent), json.dumps(draft), "owner"))
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, original_rating, text, "
                 "fetched_at, draft_response, response_status, edit_category, posted_at, edited_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','-3 days'),datetime('now','-1 days'))",
                 (rid, "google", "g1", 4, 2, "better now", "2026-09-01", "Sorry — please call.", "posted", "light"))
    conn.commit()
    conn.close()
    out = admin_ops.learning_holdouts(days=30, restaurant_id=rid)
    lines = {o["arm"]: o for o in out["orders"]}
    assert lines["applied"]["lines"] == 1 and lines["held_out"]["lines"] == 1
    assert out["reply_outcomes"] == [{"reply": "edited reply", "guest_raised": 1, "guest_lowered": 0}]
