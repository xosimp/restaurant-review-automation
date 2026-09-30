"""Memory re-audit fix round 9/29/26 (R5, demo_name_learning — INVENTORY-1,
PLATFORM-8, LOOPS-16): the name rule keeps a restaurant out of POOLED
learning only. Its own learners (models.learns_for_itself) are off only for a
demo or an account an admin excluded; a paying test-named account raises an
admin issue; the nightly pass counts what it skips; a demo's draft reads no
patterns."""
import json
import sys

import pytest

import models
from models import Restaurant, create_restaurant, update_restaurant


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


def _rid(name, **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    if kw:
        update_restaurant(rid, kw)
    return rid


def test_a_test_name_leaves_pooled_learning_but_not_its_own():
    paying = _rid("Nashville Test Kitchen", billing_status="active")
    demo = _rid("Harbor Grill")
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (demo,))
    conn.commit()
    conn.close()
    excluded = _rid("Excluded Co", learning_override="exclude")
    real = _rid("Harbor Real")
    assert models.learning_exclusion(paying) == "test_name"
    assert not models.learning_eligible(paying)                     # pooled: out
    assert models.learns_for_itself(paying)                         # its own: on
    assert not models.learns_for_itself(demo)
    assert not models.learns_for_itself(excluded)
    assert models.learns_for_itself(real) and models.learning_eligible(real)
    st = models.learning_status(paying)
    assert st["eligible"] is False and st["learns_for_itself"] is True and st["billing_history"] is True
    # every pooled reader still filters it out
    assert paying in models.learning_ineligible_ids()


def test_its_own_learners_read_its_own_record():
    import drafter
    import marketing_voice
    import review_signals
    rid = _rid("The Sample Room", billing_status="active")
    assert drafter._learns(rid)
    conn = models.get_conn()
    for i in range(4):
        conn.execute("INSERT INTO marketing_edits (restaurant_id, channel, source, ref_id, original_body, "
                     "final_body, edit_category, edit_signals, words_before, words_after, authority) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, "social", "post", f"p{i}", "Come in tonight!!", "Come in tonight.", "light",
                      json.dumps(["removed_exclamations"]), 3, 3, "principal"))
    conn.commit()
    conn.close()
    assert "THE OWNER'S VOICE" in marketing_voice.voice_block(rid, "social")
    assert review_signals.retag_examples(rid) == []          # none recorded, but not refused for the name


def test_the_nightly_pass_walks_test_named_accounts_and_counts_what_it_skips(monkeypatch):
    import learning_memory
    import scheduler
    named = _rid("Preview Pizza", billing_status="active")
    demo = _rid("Demo Grill", billing_status="active")
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (demo,))
    conn.commit()
    conn.close()
    skipped = []
    ids = learning_memory.eligible_ids(skipped=skipped)
    assert named in ids and demo not in ids and demo in skipped
    ran = []
    monkeypatch.setattr(learning_memory, "nightly", lambda rid: ran.append(rid) or {"ok": True})
    out = scheduler.run_learning_memory()
    assert named in ran and demo not in ran
    assert out["skipped"] >= 1


def test_a_demos_draft_reads_no_learned_patterns(monkeypatch):
    import schedule_versions
    demo = _rid("Harbor Demo Grill")
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (demo,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(schedule_versions, "learned_patterns",
                        lambda *a, **k: [{"kind": "moved_off", "employee": "Bob", "text": "x"}])
    assert schedule_versions.patterns_for_draft(demo) == ([], [])


def test_a_paying_test_named_account_raises_an_admin_issue(monkeypatch):
    import admin_ops
    monkeypatch.setattr(admin_ops, "get_conn", models.get_conn, raising=False)
    rid = _rid("Test Kitchen Pizza", billing_status="active")
    included = _rid("Sample Room", billing_status="active", learning_override="include")
    from auth import init_auth
    init_auth(db_path=models.DB_PATH)
    admin_ops.invalidate_fleet_cache()
    keys = [i["key"] for i in admin_ops.issues_page(segment="all", per_page=500)["items"]]
    assert f"{rid}:learning:test_name" in keys
    assert f"{included}:learning:test_name" not in keys
