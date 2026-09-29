"""Memory audit 9/29/26, workstream M1 — signatures.

Model-written advice had no stable identity: each rewording was a new hash
key, so answers did not carry over and results never accumulated. The
advice signature is now computed when a line is shown and stored on the
episode (a `signature` column and a "sig:" tag); every answer counts
against it for as long as that answer holds; present_recs suppresses a
reworded line by it; learning groups by it; Intel lines are keyed by it;
and old episodes are backfilled.
"""
import json

import pytest

import models
import rec_ledger as rl
import rec_learning
import insight_store
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    insight_store._SUBJECTS_CACHE.clear()
    yield


def _rid(db_path, name="Sig Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _one(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def test_the_three_topicless_kinds_have_a_topic():
    for kind, topic in (("insight_labor", "staffing"), ("diag_labor", "staffing"), ("content_idea", "posting")):
        assert rl.KIND_TOPIC[kind] == topic
        assert f"topic:{topic}" in rl.tags_for(f"{kind}:abcdef0123")


def test_a_shown_line_stores_its_signature_and_tag(db_path):
    rid = _rid(db_path)
    rec = rl.present(rid, "insight_labor:abcdef0123", "labor", "labor", title="Cut a server on Tuesday nights",
                     model_written=True, db_path=db_path)
    row = _one(db_path, "SELECT signature, tags FROM rec_instances WHERE rec_id=?", rec)
    assert row["signature"] == "labor:day:tuesday"
    assert "sig:labor:day:tuesday" in json.loads(row["tags"])
    # A line that names no subject is computed once and stored as ''.
    rec2 = rl.present(rid, "insight_labor:9999999999", "labor", "labor", title="Keep an eye on labor",
                      db_path=db_path)
    assert _one(db_path, "SELECT signature FROM rec_instances WHERE rec_id=?", rec2)[0] == ""


def test_an_answer_on_home_suppresses_the_same_advice_in_a_reads_new_words(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", title="Trim Tuesday staffing", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "completed", db_path=db_path)
    items = [{"key": "insight_labor:1111111111", "text": "Cut one server from Tuesday dinner"},
             {"key": "insight_labor:2222222222", "text": "Post the brunch special"}]
    kept = insight_store.present_recs(rid, "labor", "labor", items, db_path=db_path)
    assert [k["key"] for k in kept] == ["insight_labor:2222222222"]
    assert "labor:day:tuesday" in insight_store.answered_signatures(rid, db_path=db_path)


def test_a_line_names_the_restaurants_own_items(db_path):
    rid = _rid(db_path)
    c = models.get_conn(db_path)
    c.execute("INSERT INTO ingredients (restaurant_id, name, unit) VALUES (?, 'Salmon Fillet', 'lb')", (rid,))
    c.commit()
    c.close()
    subj = insight_store.known_subjects(rid, db_path=db_path)
    assert insight_store.advice_signature("insight_food:x", "Cut the salmon fillet waste this week",
                                          subjects=subj) == "waste:item:salmon fillet"
    rec = rl.present(rid, "insight_food:3333333333", "food", "food", title="Salmon fillet waste is up again",
                     db_path=db_path)
    assert _one(db_path, "SELECT signature FROM rec_instances WHERE rec_id=?", rec)[0] == "waste:item:salmon fillet"


def test_results_accumulate_under_the_signature(db_path):
    rid = _rid(db_path)
    for i, words in enumerate(("Cut a server Tuesday nights", "Trim one server from Tuesday dinner",
                               "Tuesday: drop a server")):
        key = f"insight_labor:{i:010d}"
        rl.present(rid, key, "labor", "labor", title=words, model_written=True, db_path=db_path)
        rl.record(rid, key, "accepted", db_path=db_path)
    eff = rec_learning.effectiveness(rid, db_path=db_path)
    assert eff.tags["sig:labor:day:tuesday"]["taken"] == 3


def test_backfill_fills_old_episodes(db_path):
    rid = _rid(db_path)
    rec = rl.present(rid, "insight_labor:4444444444", "labor", "labor", title="Cut a server Friday nights",
                     db_path=db_path)
    c = models.get_conn(db_path)
    c.execute("UPDATE rec_instances SET signature=NULL, tags='[]' WHERE rec_id=?", (rec,))
    c.commit()
    c.close()
    assert rl.backfill_tags(db_path=db_path) >= 1
    row = _one(db_path, "SELECT signature, tags FROM rec_instances WHERE rec_id=?", rec)
    assert row["signature"] == "labor:day:friday" and "topic:staffing" in json.loads(row["tags"])
    assert rl.backfill_tags(db_path=db_path) == 0                        # its own cursor


def test_intel_lines_are_keyed_by_what_they_are_about():
    assert insight_store.signature_key("insight_intel", "Win Friday dinner guests with a prix fixe") == \
        "insight_intel:competition:day:friday"
    # Two wordings of the same advice are one key.
    assert insight_store.signature_key("insight_intel", "Friday: go after the dinner crowd next door") == \
        "insight_intel:competition:day:friday"
    import inspect
    import client_api
    src = inspect.getsource(client_api.intel_recs_payload)
    assert 'signature_key("insight_intel"' in src and 'line_key("insight_intel"' not in src
