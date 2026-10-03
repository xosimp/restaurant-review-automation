"""Memory re-audit fix round (9/29/26), R9 — "reviews_erase" (FORGET-1).

"Removed" reviews were only soft-deleted: the guest's name, words and every
drafted reply stayed in the database and in every off-site backup forever,
and a later guest edit rewrote the hidden row. Now the soft delete is a
30-day undo window, after which the guest text is erased (the row and its
platform key stay, so a re-fetch cannot re-add it); the off-site copy blanks
removed rows at once; and an edit never touches a removed row.
"""
import sqlite3

import pytest

import models
import ops


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return models.create_restaurant(models.Restaurant(name="Erase Co", owner_email="e@x.test"))


def _review(db_path, rid, ext, deleted_days_ago=None):
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, summary, draft_response, original_draft, entities, deleted_at) "
                  "VALUES (?, 'google', ?, 'Dana Guest', 2, 'The soup was cold and Sam was rude', "
                  "'2025-01-05', '2025-01-05', 'cold soup', 'Sorry Dana', 'Sorry Dana!', '{\"staff\":[\"Sam\"]}', "
                  "CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END)",
                  (rid, ext, deleted_days_ago, f"-{deleted_days_ago or 0} days"))
        c.commit()
    finally:
        c.close()


def _row(db_path, ext):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM reviews WHERE external_id=?", (ext,)).fetchone())
    finally:
        c.close()


def test_a_removed_review_is_erased_after_its_undo_window(db_path):
    rid = _rid()
    _review(db_path, rid, "old-removed", deleted_days_ago=45)
    _review(db_path, rid, "just-removed", deleted_days_ago=3)
    _review(db_path, rid, "live")
    out = ops.prune_ledgers(db_path)
    assert out.get("reviews_erased") == 1
    gone = _row(db_path, "old-removed")
    assert gone["erased_at"] and gone["text"] == ""
    for col in ("author", "summary", "draft_response", "original_draft", "entities"):
        assert gone[col] is None, col
    assert gone["rating"] == 2 and gone["deleted_at"]         # the record, not the words
    assert _row(db_path, "just-removed")["text"].startswith("The soup")     # still undoable
    assert _row(db_path, "live")["author"] == "Dana Guest"
    # idempotent: nothing more to erase
    assert "reviews_erased" not in ops.prune_ledgers(db_path)


def test_the_erase_window_has_a_floor(db_path, monkeypatch):
    monkeypatch.setattr(ops, "REVIEW_ERASE_DAYS", 2)
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: {"sent": True})
    rid = _rid()
    _review(db_path, rid, "r", deleted_days_ago=10)
    out = ops.prune_ledgers(db_path)
    assert "reviews_erase" in [r["table"] for r in out["refused"]]
    assert _row(db_path, "r")["erased_at"] is None


def test_a_guest_edit_never_rewrites_a_removed_review(db_path):
    rid = _rid()
    _review(db_path, rid, "gx", deleted_days_ago=1)
    r = models.Review(restaurant_id=rid, platform="google", external_id="gx", author="Dana Guest", rating=1,
                      text="Edited: even worse", review_date="2025-01-05")
    models.save_reviews([r], db_path=db_path)
    row = _row(db_path, "gx")
    assert row["rating"] == 2 and row["text"].startswith("The soup")


def test_the_offsite_copy_blanks_removed_reviews_at_once(db_path, tmp_path):
    import shutil
    import offsite_backup
    rid = _rid()
    _review(db_path, rid, "hidden", deleted_days_ago=1)
    _review(db_path, rid, "shown")
    copy = str(tmp_path / "copy.db")
    import models as _m
    _m.checkpoint(db_path)          # a live file copied by hand (connections are pooled)
    shutil.copyfile(db_path, copy)
    out = offsite_backup.redact(copy)
    assert "reviews.text (removed rows)" in out["rows"]
    assert _row(copy, "hidden")["text"] == "" and _row(copy, "hidden")["author"] is None
    assert _row(copy, "shown")["author"] == "Dana Guest"
    assert offsite_backup.describe_scrub(out)["removed_rows"]
    # The local database is untouched.
    assert _row(db_path, "hidden")["author"] == "Dana Guest"
