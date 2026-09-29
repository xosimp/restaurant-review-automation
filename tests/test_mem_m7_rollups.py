"""Memory fix round (9/29/26), workstream M7 — "rollups", "review_retention",
"seasonal_food".

What outlives a prune (history_rollups): monthly alert and engagement
summaries written before alert_log / login_history / notification_opens go;
newsletter results stamped on the newsletter row 30 days after the send;
the reviews an owner's retention choice soft-deletes summarised per month in
the same transaction (no guest text), and the reply-style readers honouring
the deletion; the weekly inventory summary and monthly ingredient cost
written before inventory_history rows go, and read back by waste_trend's
loader — so the seasonal food-cost re-check reaches past 395 days.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

# Imported at collection, so a module that binds get_conn at import
# (guest_marketing — CLAUDE.md, bound imports) binds the real one, never a
# test's patch.
import guest_email  # noqa: F401
import guest_marketing  # noqa: F401
import history_rollups
import models
import ops
import value_delivered
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: {"sent": False})
    ops._claim_fallback.clear()
    yield


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _rows(sql, args=()):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _rid(name="Rollup Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _ago(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


# ── alerts: "since you started" never falls ────────────────────────────────

def test_lifetime_alerts_and_months_survive_the_alert_log_prune():
    rid = _rid()
    # One alert in each of the last 14 months, 3 in the oldest.
    for m in range(14):
        _x("INSERT INTO alert_log (restaurant_id, alert_type, fired_at, value) VALUES (?,?,?,?)",
           (rid, "one_star", _ago(30 * m + 20), 50.0))
    for _ in range(2):
        _x("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)", (rid, "labor_over", _ago(410)))
    before = value_delivered.ledger(rid)
    assert before["alerts_sent"] == 16
    # Summaries are written for whole months as the nights pass; here all at
    # once, with retention off, then the prune at 180 days.
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(ops._RETENTION_DAYS, "alert_log", 0)
        history_rollups.roll_alerts()
    ops.prune_ledgers()
    assert len(_rows("SELECT id FROM alert_log WHERE restaurant_id=?", (rid,))) < 16, "the prune ran"
    after = value_delivered.ledger(rid)
    assert after["alerts_sent"] == before["alerts_sent"] == 16
    assert after["months_active"] == before["months_active"] >= 13
    row = _rows("SELECT * FROM alert_monthly WHERE restaurant_id=? AND alert_type='labor_over'", (rid,))[0]
    assert row["fired"] == 2


def test_a_month_the_prune_has_started_on_is_never_recomputed():
    rid = _rid()
    for _ in range(4):
        _x("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)", (rid, "x", _ago(200)))
    month = _ago(200)[:7]
    _x("INSERT INTO alert_monthly (restaurant_id, month, alert_type, fired) VALUES (?,?,?,?)", (rid, month, "x", 9))
    history_rollups.roll_alerts()          # the month starts before the 180-day window: frozen
    assert _rows("SELECT fired FROM alert_monthly WHERE restaurant_id=? AND month=?", (rid, month)) == [{"fired": 9}]


def test_engagement_is_summarised_per_login_before_its_rows_go(monkeypatch):
    import auth
    auth.init_auth(db_path=models.DB_PATH)
    rid = _rid()
    _x("INSERT INTO users (id, restaurant_id, username, email, password_hash) VALUES (77, ?, 'eng', 'e@x.test', 'x')",
       (rid,))
    when = _ago(45)
    for d in (0, 0, 1):
        _x("INSERT INTO login_history (user_id, restaurant_id, created_at) VALUES (77, ?, datetime(?, ?))",
           (rid, when, f"+{d} days"))
    _x("INSERT INTO notification_opens (restaurant_id, user_id, alert_type, opened_at) VALUES (?, 77, 'one_star', ?)",
       (rid, when))
    history_rollups.roll_engagement()
    m = when[:7]
    got = _rows("SELECT logins, login_days, opens, opens_by_type FROM engagement_monthly WHERE user_id=77 AND month=?",
                (m,))
    if datetime.utcnow().strftime("%Y-%m") == m:
        assert got == []                  # the month is not over yet
    else:
        assert got == [{"logins": 3, "login_days": 2, "opens": 1, "opens_by_type": '{"one_star": 1}'}]


# ── newsletters: results outlive email_log ─────────────────────────────────

def _newsletter(rid, days_ago, opened=True):
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=models.DB_PATH)
    c = models.get_conn()
    try:
        nid = c.execute("INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total, created_at, "
                        "completed_at) VALUES (?,?,?,?,?,?,?)",
                        (rid, "Fall menu", "b", "h", 2, _ago(days_ago), _ago(days_ago))).lastrowid
        for i, mid in enumerate(("m-1", "m-2")):
            c.execute("INSERT INTO guest_newsletter_recipients (newsletter_id, contact_id, email, status, sent_at, "
                      "message_id) VALUES (?,?,?,?,?,?)", (nid, i + 1, f"g{i}@x.test", "sent", _ago(days_ago), mid))
        c.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, message_id, opened_at, "
                  "clicked_at) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, "guest_newsletter", "g0@x.test", "Fall menu", _ago(days_ago), "m-1",
                   _ago(days_ago - 1) if opened else None, None))
        c.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, message_id) "
                  "VALUES (?,?,?,?,?,?)", (rid, "guest_newsletter", "g1@x.test", "Fall menu", _ago(days_ago), "m-2"))
        # Open tracking is on: an open inside the last 90 days, platform-wide.
        c.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, opened_at) "
                  "VALUES (?,?,?,?,?,?)", (rid, "digest", "o@x.test", "d", _ago(3), _ago(2)))
        c.commit()
    finally:
        c.close()
    return nid


def test_a_year_old_newsletter_keeps_its_measured_opens_after_email_log_ages_out():
    import guest_email
    rid = _rid()
    nid = _newsletter(rid, 400)
    assert history_rollups.stamp_newsletter_results()["stamped"] == 1
    _x("DELETE FROM email_log WHERE message_id IN ('m-1','m-2')")        # what the 365-day prune does
    item = [n for n in guest_email.newsletter_history(rid) if n["id"] == nid][0]
    assert (item["opened"], item["clicked"], item["tracked"]) == (1, 0, 2)
    assert item["results_as_of"]


def test_a_recent_newsletter_is_read_live_and_not_stamped_yet():
    import guest_email
    rid = _rid()
    nid = _newsletter(rid, 5)
    assert history_rollups.stamp_newsletter_results()["stamped"] == 0
    item = [n for n in guest_email.newsletter_history(rid) if n["id"] == nid][0]
    assert item["opened"] == 1 and "results_as_of" not in item


def test_opens_unknown_when_stamped_stay_unknown(monkeypatch):
    import guest_email
    rid = _rid()
    nid = _newsletter(rid, 60)
    monkeypatch.setattr(guest_email, "opens_tracked", lambda db_path=None: False)
    history_rollups.stamp_newsletter_results()
    monkeypatch.setattr(guest_email, "opens_tracked", lambda db_path=None: True)
    item = [n for n in guest_email.newsletter_history(rid) if n["id"] == nid][0]
    assert item["opened"] is None and item["clicked"] is None, "an unknown is never a measured zero"


# ── reviews: the owner's retention forgets the words, not the record ───────

def _review(rid, ext, days_ago, rating, status="posted", draft="Thanks!", action=None, edit=None):
    _x("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, "
       "draft_response, response_status, response_action, edit_category, approved_at) "
       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
       (rid, "google", ext, "Guest Name", rating, "The guest's own words", _ago(days_ago), _ago(days_ago), draft,
        status, action, edit, _ago(days_ago)))


def test_the_purge_writes_monthly_stats_first_and_the_lifetime_counts_hold():
    rid = _rid()
    _x("UPDATE restaurants SET data_retention_months=6 WHERE id=?", (rid,))
    _review(rid, "old-1", 400, 2, edit="heavy")
    _review(rid, "old-2", 400, 4)
    _review(rid, "new-1", 10, 5)
    before = value_delivered.ledger(rid)
    assert models.purge_expired_reviews() == 2
    after = value_delivered.ledger(rid)
    for k in ("replies_drafted", "replies_posted", "reviews_watched"):
        assert after[k] == before[k], k
    stats = _rows("SELECT * FROM review_monthly_stats WHERE restaurant_id=?", (rid,))
    assert len(stats) == 1 and stats[0]["reviews"] == 2 and stats[0]["rating_sum"] == 6 and stats[0]["posted"] == 2
    assert json.loads(stats[0]["edits_json"]) == {"heavy": 1}
    cols = {r["name"] for r in _rows("PRAGMA table_info(review_monthly_stats)")}
    assert not cols & {"text", "author", "draft_response", "summary"}, "no guest text in the summary"
    months = history_rollups.review_months(rid)
    old = [m for m in months if m["purged"]][0]
    assert old["reviews"] == 2 and old["avg_rating"] == 3.0
    assert models.purge_expired_reviews() == 0
    assert _rows("SELECT reviews FROM review_monthly_stats WHERE restaurant_id=?", (rid,)) == [{"reviews": 2}]


def test_the_reply_style_readers_honour_the_owners_retention():
    rid = _rid()
    _x("UPDATE restaurants SET data_retention_months=6 WHERE id=?", (rid,))
    _review(rid, "old-edited", 800, 5, status="approved", draft="Old style reply", edit="light")
    _review(rid, "new", 10, 4, status="approved", draft="New style reply", edit="light")
    models.purge_expired_reviews()
    examples = models.get_approved_examples(rid)
    assert [e["response"] for e in examples] == ["New style reply"]
    assert len(models.get_reply_edit_summaries(rid)) == 1


# ── inventory: summarised before it goes, read back after ──────────────────

def _snapshot(rid, day, inv_value, waste, items):
    _x("INSERT INTO inventory_history (restaurant_id, week_end, waste_json, items_json, inv_value, source) "
       "VALUES (?,?,?,?,?,?)", (rid, day, json.dumps({"total_waste_cost": waste, "top_items": ["Salmon"]}),
                                json.dumps(items), inv_value, "scheduled"))


def test_old_inventory_is_summarised_before_it_goes_and_read_back_after():
    import waste_trend
    import cogs
    rid = _rid()
    old_day = (date.today() - timedelta(days=500)).isoformat()
    next_day = (date.today() - timedelta(days=493)).isoformat()
    salmon = [{"ingredient_id": 7, "item": "Salmon", "unit": "lb", "unit_cost": 12.5, "supplier_name": "Sysco"}]
    _snapshot(rid, old_day, 4000.0, 120.0, salmon)
    _snapshot(rid, next_day, 4200.0, 90.0, [dict(salmon[0], unit_cost=13.5)])
    ops.prune_ledgers()
    assert _rows("SELECT id FROM inventory_history WHERE restaurant_id=?", (rid,)) == [], "raw rows went"
    summary = _rows("SELECT week_end, inv_value, waste_value FROM inventory_weekly_summary WHERE restaurant_id=? "
                    "ORDER BY week_end", (rid,))
    assert [(s["week_end"], s["inv_value"], s["waste_value"]) for s in summary] == \
        [(old_day, 4000.0, 120.0), (next_day, 4200.0, 90.0)]
    weeks, total = waste_trend.load_waste_history(rid, since=old_day, until=next_day)
    assert [w["inv_value"] for w in weeks] == [4000.0, 4200.0] and total == 2
    assert cogs.inventory_value_near(weeks, date.fromisoformat(old_day)) == (4000.0, old_day)
    hist = history_rollups.ingredient_cost_history(rid, "Salmon")
    assert [(h["month"], h["unit_cost_last"]) for h in hist][-1][1] == 13.5
    assert hist[0]["unit"] == "lb" and hist[0]["supplier"] == "Sysco"


def test_a_week_whose_summary_was_not_written_is_not_deleted(monkeypatch):
    rid = _rid()
    old_day = (date.today() - timedelta(days=500)).isoformat()
    _snapshot(rid, old_day, 4000.0, 120.0, [])
    monkeypatch.setattr(history_rollups, "summarise_inventory", lambda conn, before, deadline=None: 0)
    ops.prune_ledgers()
    assert len(_rows("SELECT id FROM inventory_history WHERE restaurant_id=?", (rid,))) == 1


def test_recent_weeks_are_read_from_the_raw_rows_not_the_summary():
    import waste_trend
    rid = _rid()
    day = (date.today() - timedelta(days=20)).isoformat()
    _snapshot(rid, day, 5000.0, 60.0, [])
    _x("INSERT INTO inventory_weekly_summary (restaurant_id, week, week_end, inv_value, waste_json) VALUES (?,?,?,?,?)",
       (rid, history_rollups._iso_monday(day), day, 1.0, "{}"))
    weeks, total = waste_trend.load_waste_history(rid)
    assert [w["inv_value"] for w in weeks] == [5000.0] and total == 1
