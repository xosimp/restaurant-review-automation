"""Reaudit H: Home, the recommendation ledger and the admin acceptance page.

Each test names the finding it pins (H-1 … H-27). The thread through most of
them is rec_ledger: an answer belongs to the episode the owner was looking at
when they gave it, an episode nobody was shown is not a recommendation, and
the same news is said once."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

import action_queue
import admin_ops
import auth
import business_intelligence as bi
import client_api
import decisions
import home_brief
import mobile_api
import models
import morning_brief
import outcomes
import rec_ledger
import strategy_routes
from auth import init_auth
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import review_intelligence, food_cost_intelligence, issues, metrics
    from intelligence import feedback
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, home_brief, bi, review_intelligence, admin_ops,
                food_cost_intelligence, morning_brief, action_queue, outcomes, issues, metrics, feedback):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    import ai_utils
    c = get_conn(db_path); c.executescript(ai_utils._USAGE_TABLE_SQL); c.commit(); c.close()
    home_brief.invalidate()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called")))


def _rid(db_path, **kw):
    fields = dict(name="Ledger Co", owner_email="o@x.test", owner_name="Sam Owner", module_reviews=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _user(rid, uid=1, role="owner"):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": role,
            "is_admin": 0, "email": "o@x.test"}


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ago(**kw):
    return _stamp(datetime.utcnow() - timedelta(**kw))


def _episode(db_path, rid, key, created, shown=True, status="open", kind=None):
    """An episode created at `created`, shown then."""
    rec_id = uuid.uuid4().hex
    c = get_conn(db_path)
    c.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, kind, status, created_at, last_event_at) "
              "VALUES (?,?,?,?,?,?,?)", (rec_id, rid, key, kind or key.split(":")[0], status, created, created))
    if shown:
        c.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, at) "
                  "VALUES (?,?,?,'shown','home',?,?)", (rec_id, rid, key, f"shown:home:{created[:10]}", created))
    c.commit(); c.close()
    return rec_id


def _row(db_path, rec_id):
    c = get_conn(db_path)
    r = c.execute("SELECT * FROM rec_instances WHERE rec_id=?", (rec_id,)).fetchone()
    c.close()
    return r


def _events(db_path, rid, key, event=None):
    c = get_conn(db_path)
    sql = "SELECT * FROM rec_events WHERE restaurant_id=? AND key=?"
    args = [rid, key]
    if event:
        sql += " AND event=?"
        args.append(event)
    rows = c.execute(sql, args).fetchall()
    c.close()
    return rows


def _home_row(db_path, rid, key, kind, dismissed_at, expires_at):
    c = get_conn(db_path)
    c.execute("INSERT INTO home_dismissals (restaurant_id, key, kind, dismissed_at, expires_at) VALUES (?,?,?,?,?)",
              (rid, key, kind, dismissed_at, expires_at))
    c.commit(); c.close()


# ── H-1 the nightly sync ─────────────────────────────────────────────────────

def test_h1_a_home_not_today_stays_a_snooze_through_the_sync(db_path):
    rid = _rid(db_path)
    rec = rec_ledger.present(rid, "rating_drop", "reviews", "home", db_path=db_path)
    home_brief.dismiss(rid, "rating_drop", kind="snooze")
    rec_ledger.sync_existing(db_path=db_path)
    r = _row(db_path, rec)
    assert r["status"] == "open" and r["silenced_until"] is None
    assert not _events(db_path, rid, "rating_drop", "dismissed")
    assert len(_events(db_path, rid, "rating_drop", "snoozed")) == 1


def test_h1_a_snooze_the_ledger_never_saw_is_carried_as_a_snooze_to_its_own_expiry(db_path):
    rid = _rid(db_path)
    rec = _episode(db_path, rid, "rating_drop", _ago(hours=2))
    until = _stamp(datetime.utcnow() + timedelta(hours=20))
    _home_row(db_path, rid, "rating_drop", "snooze", _ago(minutes=30), until)
    assert rec_ledger.sync_existing(db_path=db_path).get("home") == 1
    r = _row(db_path, rec)
    assert r["status"] == "open" and r["snoozed_until"] == until and r["silenced_until"] is None


def test_h1_an_old_answer_never_closes_an_episode_shown_after_it(db_path):
    rid = _rid(db_path)
    # A hide given 20 days ago whose fortnight has lapsed …
    _home_row(db_path, rid, "top_issue:service", "recommendation", _ago(days=20), _ago(days=6))
    # … and the recommendation shown again today.
    rec = rec_ledger.present(rid, "top_issue:service", "reviews", "home", db_path=db_path)
    rec_ledger.sync_existing(db_path=db_path)
    rec_ledger.sync_existing(db_path=db_path)
    r = _row(db_path, rec)
    assert r["status"] == "open" and r["silenced_until"] is None
    assert not rec_ledger.silenced(rid, "top_issue:service", db_path=db_path)


def test_h1_an_old_answer_lands_on_the_episode_it_answered_and_silences_from_then(db_path):
    rid = _rid(db_path)
    old = _episode(db_path, rid, "top_issue:service", _ago(days=21))
    _home_row(db_path, rid, "top_issue:service", "recommendation", _ago(days=20), _ago(days=6))
    new = rec_ledger.present(rid, "top_issue:service", "reviews", "home", db_path=db_path)
    assert rec_ledger.sync_existing(db_path=db_path).get("home") == 1
    assert [e["rec_id"] for e in _events(db_path, rid, "top_issue:service", "dismissed")] == [old]
    assert _row(db_path, old)["silenced_until"] < _stamp(datetime.utcnow())     # the lapsed fortnight
    assert _row(db_path, new)["status"] == "open"
    # and never again, on any episode
    assert rec_ledger.sync_existing(db_path=db_path).get("home") is None


def test_h1_a_source_ref_is_one_answer_across_every_episode(db_path):
    rid = _rid(db_path)
    assert rec_ledger.record(rid, "k:1", "dismissed", source_ref="x", db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE rec_instances SET silenced_until=NULL, status='expired'")
    c.commit(); c.close()
    rec_ledger.present(rid, "k:1", "home", "home", db_path=db_path)
    assert not rec_ledger.record(rid, "k:1", "dismissed", source_ref="x", db_path=db_path)
    assert len(_events(db_path, rid, "k:1", "dismissed")) == 1


def test_h1_an_answer_home_wrote_itself_is_not_recorded_again(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "add_brunch", "home", "home", db_path=db_path)
    home_brief.dismiss(rid, "add_brunch", kind="not_for_us")
    assert rec_ledger.sync_existing(db_path=db_path).get("home") is None
    assert len(_events(db_path, rid, "add_brunch", "dismissed")) == 1


def test_h1_repair_reopens_an_episode_the_replay_closed(db_path):
    rid = _rid(db_path)
    key = "top_issue:service"
    answered = _ago(days=20)
    _home_row(db_path, rid, key, "recommendation", answered, _ago(days=6))
    rec = _episode(db_path, rid, key, _ago(days=2))
    # What the old sync wrote: the 20-day-old hide, replayed onto today's episode.
    c = get_conn(db_path)
    c.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, meta, at) "
              "VALUES (?,?,?,'dismissed','home',?,?,?)",
              (rec, rid, key, f"dismissed:home:{key}:{answered}", json.dumps({"kind": "hide"}), _ago(hours=1)))
    c.execute("UPDATE rec_instances SET status='dismissed', closed_at=?, silenced_until=datetime('now','+14 days') "
              "WHERE rec_id=?", (_ago(hours=1), rec))
    c.commit(); c.close()
    out = rec_ledger.repair_sync_replays(db_path=db_path)
    assert out == {"removed": 1, "reopened": 1}
    r = _row(db_path, rec)
    assert r["status"] == "open" and r["silenced_until"] is None and r["closed_at"] is None
    # a one-off (marked done), idempotent even when forced, and the fixed
    # sync does not put it back
    assert rec_ledger.repair_sync_replays(db_path=db_path) == {"removed": 0, "reopened": 0}
    assert rec_ledger.repair_sync_replays(db_path=db_path, force=True) == {"removed": 0, "reopened": 0}
    rec_ledger.sync_existing(db_path=db_path)
    assert _row(db_path, rec)["status"] == "open"


def test_h1_repair_undoes_a_snooze_replayed_as_a_dismissal(db_path):
    rid = _rid(db_path)
    rec = rec_ledger.present(rid, "rating_drop", "reviews", "home", db_path=db_path)
    home_brief.dismiss(rid, "rating_drop", kind="snooze")
    c = get_conn(db_path)
    at = c.execute("SELECT dismissed_at FROM home_dismissals WHERE key='rating_drop'").fetchone()[0]
    c.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, meta) "
              "VALUES (?,?,?,'dismissed','home',?,?)",
              (rec, rid, "rating_drop", f"dismissed:home:rating_drop:{at}", json.dumps({"kind": "hide"})))
    c.execute("UPDATE rec_instances SET status='dismissed', silenced_until=datetime('now','+14 days') WHERE rec_id=?",
              (rec,))
    c.commit(); c.close()
    assert rec_ledger.repair_sync_replays(db_path=db_path)["reopened"] == 1
    assert _row(db_path, rec)["status"] == "open"


def test_h1_repair_leaves_a_correct_synced_answer_alone(db_path):
    rid = _rid(db_path)
    key = "top_issue:service"
    rec = _episode(db_path, rid, key, _ago(days=3))
    answered = _ago(days=2)
    _home_row(db_path, rid, key, "not_for_us", answered, _stamp(datetime.utcnow() + timedelta(days=3000)))
    assert rec_ledger.sync_existing(db_path=db_path).get("home") == 1
    assert rec_ledger.repair_sync_replays(db_path=db_path)["removed"] == 0
    assert _row(db_path, rec)["status"] == "dismissed"


# ── H-2 expiry from creation ────────────────────────────────────────────────

def test_h2_a_card_shown_every_day_still_expires_two_weeks_after_it_began(db_path):
    rid = _rid(db_path)
    rec = rec_ledger.present(rid, "post_this_week", "marketing", "home", db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE rec_instances SET created_at=datetime('now','-30 days'), last_event_at=datetime('now','-1 days') "
              "WHERE rec_id=?", (rec,))
    c.commit(); c.close()
    # the admin page reads it as ignored by the same rule …
    eps = admin_ops._episodes(get_conn(db_path), _ago(days=60), rid)
    assert eps[0]["ignored"]
    # … and the nightly job closes it
    assert rec_ledger.expire_stale(db_path=db_path) == 1
    assert _row(db_path, rec)["status"] == "expired"


def test_h2_a_showing_after_two_weeks_starts_a_new_episode(db_path):
    rid = _rid(db_path)
    rec = rec_ledger.present(rid, "post_this_week", "marketing", "home", db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE rec_instances SET created_at=datetime('now','-15 days') WHERE rec_id=?", (rec,))
    c.commit(); c.close()
    again = rec_ledger.present(rid, "post_this_week", "marketing", "home", db_path=db_path)
    assert again != rec and _row(db_path, rec)["status"] == "expired"


def test_h2_a_snoozed_episode_does_not_expire_inside_its_snooze(db_path):
    rid = _rid(db_path)
    rec = rec_ledger.present(rid, "reprice:Carbonara", "food", "queue", db_path=db_path)
    rec_ledger.record(rid, "reprice:Carbonara", "snoozed", snooze_until="2999-01-01 00:00:00", db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE rec_id=?", (rec,))
    c.commit(); c.close()
    assert rec_ledger.expire_stale(db_path=db_path) == 0


# ── H-3 quieter stays quiet ─────────────────────────────────────────────────

def test_h3_a_quiet_kind_stays_quiet_after_the_next_build_shows_it(db_path):
    rid = _rid(db_path)
    for i in range(4):
        _episode(db_path, rid, "post_this_week", _ago(days=60 - i * 14), status="expired")
    assert decisions.quiet_kinds(rid, db_path=db_path) == {"post_this_week"}
    rec_ledger.present(rid, "post_this_week", "marketing", "home", db_path=db_path)     # the next build
    assert decisions.quiet_kinds(rid, db_path=db_path) == {"post_this_week"}


# ── H-4 Track this is taking it; outcome rate over what was taken ────────────

def test_h4_track_this_records_the_recommendation_as_accepted(db_path, monkeypatch):
    rid = _rid(db_path)
    rec = rec_ledger.present(rid, "trim_day:Friday", "labor", "home")
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"source": "recommendation", "source_key": "trim_day:Friday",
                                                            "title": "Trim Friday", "metric": "labor_pct"})
    out, status = strategy_routes._do_outcome_record(_user(rid))
    assert status == 200 and out["ok"]
    assert _row(db_path, rec)["status"] == "accepted"
    assert rec_ledger.silenced(rid, "trim_day:Friday", db_path=db_path)


def test_h4_outcome_rate_counts_improved_only_among_what_was_taken(db_path):
    rid = _rid(db_path)
    rec_ledger.present_many(rid, [dict(key="k:0", module="labor"), dict(key="k:1", module="labor")], "home")
    rec_ledger.record(rid, "k:0", "accepted")
    rec_ledger.record(rid, "k:0", "outcome", meta={"verdict": "improved"})
    rec_ledger.record(rid, "k:1", "outcome", meta={"verdict": "improved"})       # never taken
    t = admin_ops.recommendation_acceptance(days=30, restaurant_id=rid)["total"]
    assert t["improved"] == 1 and t["outcome_rate"] == 1.0


# ── H-5 only what was shown counts ──────────────────────────────────────────

def test_h5_an_outcome_or_open_never_starts_an_episode(db_path):
    rid = _rid(db_path)
    assert not rec_ledger.record(rid, "observed:alert_x:2026-09", "outcome", meta={"verdict": "improved"},
                                 db_path=db_path)
    assert not rec_ledger.record(rid, "never_shown", "opened", db_path=db_path)
    c = get_conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM rec_instances WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    c.close()


def test_h5_the_sync_skips_observed_trackers(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
              "evaluate_on, verdict, status) VALUES (?,?,?,?,?,?,?,?,?)",
              (rid, "observed", "observed:alert_labor_over:2026-09", "t", "labor_pct", "2026-09-01", "2026-09-20",
               "improved", "evaluated"))
    c.commit(); c.close()
    rec_ledger.sync_existing(db_path=db_path)
    assert not _events(db_path, rid, "observed:alert_labor_over:2026-09")


def test_h5_bookkeeping_and_unshown_episodes_stay_out_of_the_rates(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "k:shown", "home", "home")
    decisions.restore_kind(rid, "post_this_week")                                 # bookkeeping
    rec_ledger.record(rid, "calibration:weights", "accepted")                     # bookkeeping
    rec_ledger.record(rid, "alert_only", "dismissed", meta={"kind": "hide"})      # answered, never shown
    t = admin_ops.recommendation_acceptance(days=30, restaurant_id=rid)["total"]
    assert t["n"] == 1 and t["accepted"] == 0


# ── H-6 schedule answers on the key they were shown under ───────────────────

def test_h6_a_long_schedule_recommendation_syncs_onto_its_shown_key(db_path):
    import schedule_intel
    rid = _rid(db_path)
    text = ("Rate the unscored staff; several shift figures are reading low only because nothing was "
            "counted for them.")
    assert len(text) > 100
    key = schedule_intel.schedule_rec_key("rating", text)
    rec = rec_ledger.present(rid, key, "schedule", "schedule_review", db_path=db_path)
    schedule_intel.record_recommendation(rid, "rating", text, "accepted", db_path=db_path)
    rec_ledger.sync_existing(db_path=db_path)
    assert _row(db_path, rec)["status"] == "accepted"
    c = get_conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM rec_instances WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
    c.close()


# ── H-7 complaint share on multi-word categories ────────────────────────────

def test_h7_complaint_share_matches_a_multi_word_category(db_path):
    import metrics
    rid = _rid(db_path)
    c = get_conn(db_path)
    for i in range(10):
        cats = ["food_quality"] if i < 6 else ["service"]
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, sentiment, response_status, processed, categories) VALUES "
                  "(?, 'google', ?, 'A', 2, 't', date('now','-2 days'), datetime('now'), 'negative', 'pending', 1, ?)",
                  (rid, uuid.uuid4().hex, json.dumps(cats)))
    c.commit(); c.close()
    start, end = (date.today() - timedelta(days=10)).isoformat(), date.today().isoformat()
    by_label, _ = metrics.measure(rid, "complaints:food quality", start, end, db_path=db_path)
    by_id, _ = metrics.measure(rid, "complaints:food_quality", start, end, db_path=db_path)
    assert by_label == by_id == 60.0
    assert 'metric=f"complaints:{cat}"' in open("home_brief.py").read()


# ── H-9 iOS pulse never shows sample figures as the restaurant's own ────────

def test_h9_the_pulse_for_sample_labor_and_inventory_carries_no_tone():
    r = Restaurant(name="x", owner_email="x@x.test", labor_target_pct=30.0)
    labor = mobile_api._home_pulse("labor", {"value": "—", "sublabel": "add your shifts"}, {},
                                   {"is_live": False, "overall_labor_pct": 36.8}, r, {})
    assert labor["tone"] is None and "36.8" not in labor["label"] and "over" not in labor["label"]
    inv = mobile_api._home_pulse("inventory", {"value": "$75", "sublabel": "recoverable / mo"}, {}, None, r,
                                 {"recoverable_monthly": 75}, inv_live=False)
    assert inv["tone"] is None and inv["value"] == "—"


# ── H-11 urgent means a reply still owed ────────────────────────────────────

def test_h11_imported_old_urgent_reviews_are_not_a_critical_home_item(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    for _ in range(2):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, sentiment, urgency, response_status, processed) VALUES "
                  "(?, 'google', ?, 'A', 1, 't', date('now','-900 days'), datetime('now'), 'negative', 'high', "
                  "'pending', 1)", (rid, uuid.uuid4().hex))
    c.commit(); c.close()
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert not any(a["key"] in ("urgent_reviews", "stale_low_reviews") for a in p["attention"])


# ── H-12 a Home "Done" result is credited to its own kind ───────────────────

def test_h12_a_done_tracker_counts_under_the_recommendation_kind(db_path):
    from intelligence import feedback
    rid = _rid(db_path)
    c = get_conn(db_path)
    c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
              "evaluate_on, verdict, status) VALUES (?,?,?,?,?,?,?,?,?)",
              (rid, "observed", "trim_day:Monday", "Trim Monday", "labor_pct", "2026-08-01", "2026-08-29",
               "improved", "evaluated"))
    c.commit(); c.close()
    feedback.sync(db_path=db_path)
    c = get_conn(db_path)
    kinds = {r[0] for r in c.execute("SELECT rec_kind FROM intel_rec_events WHERE restaurant_id=?", (rid,))}
    c.close()
    assert kinds == {"trim_day"}


# ── H-13 one key per recommendation ─────────────────────────────────────────

def test_h13_the_whole_schedule_gap_is_not_keyed_as_one_day(db_path):
    rid = _rid(db_path)
    data = {"labor": {"is_live": True, "potential_savings_monthly": 900, "labor_target": 30, "period_days": 28,
                      "dow_summary": {"Monday": 28.0, "Friday": 36.0}}}
    c = next(x for x in bi.one_thing_candidates(rid, data, [], db_path=db_path) if x["modules"] == ["labor"])
    assert not c["key"].startswith("trim_day:") and c["same_as"] == "money:labor"


def test_h13_answering_one_low_item_leaves_the_others_on_the_brief(db_path, monkeypatch):
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(morning_brief, "_critical_low", lambda _rid: ["Salmon", "Chicken"])
    rec_ledger.record(rid, "stock_low:Salmon", "dismissed", meta={"kind": "hide"}, db_path=db_path)
    brief = morning_brief.build(rid, db_path=db_path)
    line = next(l for l in brief["lines"] if l["key"] == "stock")
    assert line["text"] == "Running low: Chicken." and line["rec"] == "stock_low:Chicken"


def test_h13_hiding_the_low_stock_card_answers_every_item_on_it(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(home_brief, "_current_stock_keys", lambda _rid: ["stock_low:Salmon", "stock_low:Chicken"])
    home_brief.dismiss(rid, "stock_low:Salmon", kind="snooze")
    quiet = rec_ledger.silenced_keys(rid)
    assert {"stock_low:Salmon", "stock_low:Chicken"} <= quiet


def test_h13_use_again_on_the_low_stock_card_restores_every_item(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(home_brief, "_current_stock_keys", lambda _rid: ["stock_low:Salmon", "stock_low:Chicken"])
    home_brief.dismiss(rid, "stock_low:Salmon", kind="snooze")
    home_brief.undismiss(rid, "stock_low:Salmon")
    assert not ({"stock_low:Salmon", "stock_low:Chicken"} & rec_ledger.silenced_keys(rid))


# ── H-14 the same news once ─────────────────────────────────────────────────

def test_h14_the_brief_says_one_piece_of_news_once():
    lines = [{"key": "fix_first", "rec": "schedule_to_target:30%", "same": ["money:labor"], "text": "a"},
             {"key": "money", "rec": "money:labor", "text": "b"},
             {"key": "fix_first2", "rec": "urgent_reviews", "text": "c"},
             {"key": "reviews", "rec": "no_response", "same": ["urgent_reviews"], "text": "d"},
             {"key": "yesterday", "text": "e"}, {"key": "today", "text": "f"}]
    # The money line is the one thing's figure again; the one thing about
    # low-star replies is covered by the fuller reviews line.
    assert [l["text"] for l in morning_brief._one_line_per_news(lines)] == ["a", "d", "e", "f"]
    dup = [{"key": "a", "rec": "k"}, {"key": "b", "rec": "k"}]
    assert [l["key"] for l in morning_brief._one_line_per_news(dup)] == ["a"]


def test_h14_home_does_not_repeat_the_drafted_replies_as_a_card(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    for _ in range(4):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, sentiment, response_status, processed, draft_response) VALUES "
                  # A drafted review has its draft: Home counts only replies a
                  # publish can post (models.reply_queue_counts, re-audit M-2).
                  "(?, 'google', ?, 'A', 5, 't', date('now','-2 days'), datetime('now'), 'positive', 'drafted', 1, "
                  "'Thank you!')",
                  (rid, uuid.uuid4().hex))
    c.commit(); c.close()
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert any(a["key"] == "awaiting_approval" for a in p["attention"])
    assert not any(r["key"] == "publish_drafts" for r in p["recommendations"])
    # hiding the item does not bring the same news back as a card
    home_brief.dismiss(rid, "no_response")
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert not any(r["key"] == "publish_drafts" for r in p["recommendations"])


# ── H-15 "today" is the restaurant's day ────────────────────────────────────

def test_h15_shown_today_starts_at_local_midnight(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    c.execute("UPDATE restaurants SET timezone='America/Chicago' WHERE id=?", (rid,))
    c.commit()
    midnight = decisions._local_midnight_utc(c, rid)
    c.close()
    local = datetime.strptime(midnight, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    assert local.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M:%S") == "00:00:00"
    rec = rec_ledger.present(rid, "rating_drop", "reviews", "home", db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE rec_events SET at=datetime(?, '-1 hours') WHERE rec_id=?", (midnight, rec))
    c.commit(); c.close()
    assert decisions.shown_elsewhere_today(rid, ["rating_drop"], "brief_email", db_path=db_path) == set()


# ── H-16 only what the clients render is logged as shown ────────────────────

def test_h16_home_logs_as_shown_exactly_what_its_payload_carries(db_path, monkeypatch):
    rid = _rid(db_path)
    c = get_conn(db_path)
    for i in range(12):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, sentiment, urgency, response_status, processed) VALUES "
                  "(?, 'google', ?, 'A', ?, 't', date('now','-2 days'), datetime('now','-3 days'), ?, ?, ?, 1)",
                  (rid, uuid.uuid4().hex, 1 if i < 3 else 5, "negative" if i < 3 else "positive",
                   "high" if i < 2 else "normal", "drafted" if i < 8 else "pending"))
    c.commit(); c.close()
    monkeypatch.setattr(home_brief, "HOME_ATTENTION_SHOWN", 2)
    monkeypatch.setattr(home_brief, "HOME_RECS_SHOWN", 1)
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert len(p["attention"]) > 2 and len(p["recommendations"]) <= 1
    c = get_conn(db_path)
    logged = {r[0] for r in c.execute("SELECT key FROM rec_events WHERE restaurant_id=? AND event='shown'", (rid,))}
    c.close()
    assert logged == {a["rec_key"] for a in p["attention"][:2]} | {r["key"] for r in p["recommendations"]}


def test_h16_the_server_caps_match_what_web_and_ios_render():
    assert home_brief.HOME_ATTENTION_SHOWN == 4 and home_brief.HOME_RECS_SHOWN == 3
    web = open("templates/dashboard.html").read()
    # web: the focus card takes the first item, then three rows / three cards
    assert "Math.min(items.length,3)" in web and "Math.min(recs.length,3)" in web
    ios = open("ios/CavnarAI/CavnarAI/Features/Home/HomeRecommendations.swift").read()
    assert ".prefix(3)" in ios
    home = open("ios/CavnarAI/CavnarAI/Features/Home/HomeView.swift").read()
    assert "summary.needsAttention.prefix(4)" in home


# ── H-17 the on-call ask is recorded only once it went out ──────────────────

def test_h17_a_failed_on_call_email_can_be_retried(db_path, monkeypatch):
    import shift_requests
    rid = _rid(db_path)
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"date": "2026-10-02", "employee": "Dana"})
    monkeypatch.setattr(shift_requests, "_contacts",
                        lambda *_a: {"dana": {"email": "d@x.test", "employee_name": "Dana"}})
    sends = iter([0, 1])
    monkeypatch.setattr(shift_requests, "_email_staff", lambda *a, **k: next(sends))
    out, status = strategy_routes._do_standby_ask(_user(rid))
    assert status == 502
    out, status = strategy_routes._do_standby_ask(_user(rid))
    assert status == 200 and out["sent"] == 1 and not out.get("already")
    out, status = strategy_routes._do_standby_ask(_user(rid))
    assert out.get("already") is True


# ── H-18 Ask reads every answer ─────────────────────────────────────────────

def test_h18_decisions_read_the_ledger_snoozes_and_dates(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "reprice:Carbonara", "food", "food", title="Reprice Carbonara", db_path=db_path)
    rec_ledger.record(rid, "reprice:Carbonara", "dismissed", surface="food", meta={"kind": "not_for_us"},
                      db_path=db_path)
    home_brief.dismiss(rid, "rating_drop", kind="snooze")
    rows = {r["key"]: r for r in decisions.history(rid, db_path=db_path)}
    assert rows["reprice:Carbonara"]["answer"] == "not for us"
    assert rows["rating_drop"]["answer"] == "snoozed"
    text = decisions.context(rid, db_path=db_path)
    from time_utils import mdy
    assert f"({mdy(datetime.utcnow().date())})" in text
    assert datetime.utcnow().date().isoformat() not in text


# ── H-19 the monthly review follows the viewer's permissions ────────────────

def test_h19_the_monthly_review_hides_food_cost_dollars_from_a_login_without_food(db_path, monkeypatch):
    import monthly_review
    rid = _rid(db_path)
    review = {"month": "August 2026", "metrics": [], "results": [], "goals": [{"metric": "food_cost_pct"}],
              "prime_cost": {"pct": 61.0}, "compared_with": "July",
              "priorities": [{"key": "money:food_cost", "monthly": 900}, {"key": "money:labor", "monthly": 400}],
              "fix_first": {"key": "cut_waste:Salmon", "modules": ["food_cost"], "what": "Cut salmon"}}
    monkeypatch.setattr(monthly_review, "build", lambda *a, **k: dict(review))
    monkeypatch.setattr(strategy_routes, "_sees_food", lambda u: False)
    out, _ = strategy_routes._do_monthly_review(_user(rid, role="manager"))
    r = out["review"]
    assert [p["key"] for p in r["priorities"]] == ["money:labor"]
    assert r["fix_first"] is None and r["prime_cost"] is None and r["goals"] == []


def test_h19_monthly_impressions_are_logged_as_the_monthly_email(db_path, monkeypatch):
    import monthly_review, review_common
    rid = _rid(db_path)
    seen = {}
    monkeypatch.setattr(review_common, "present", lambda *a, **k: seen.update(k))
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {})
    monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    assert seen.get("surface") == "monthly_email"
    assert "monthly_email" in rec_ledger.SURFACES


# ── H-20 / H-25 dates an owner reads ────────────────────────────────────────

def test_h20_the_new_schedule_change_line_is_dated_mdy():
    src = open("home_brief.py").read()
    assert "New schedule built for {_mdy(last_schedule.get('week_start'))" in src


def test_h25_the_weekly_review_names_its_week_in_mdy(db_path, monkeypatch):
    import weekly_review
    rid = _rid(db_path)
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {})
    r = weekly_review.build(rid, today=date(2026, 9, 23), db_path=db_path)
    assert r["week"] == "9/14/26 – 9/20/26" and r["compared_with"] == "9/7/26 – 9/13/26"


def test_h25_the_brief_email_does_not_call_the_weather_measured():
    brief = {"date": "2026-09-23", "lines": [{"key": "today", "tone": "neutral", "outside": True,
                                              "text": "Today — 88° and sunny."}]}
    html = morning_brief._email_html(brief, "R")
    assert "except the weather and the calendar" in html
    plain = morning_brief._email_html({"date": "2026-09-23", "lines": [{"key": "x", "tone": "neutral",
                                                                         "text": "Yesterday: $900."}]}, "R")
    assert "except the weather" not in plain


# ── H-21 a queue snooze ends at local midnight ──────────────────────────────

def test_h21_a_queue_snooze_ends_at_the_restaurants_midnight(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    c.execute("UPDATE restaurants SET timezone='America/Chicago' WHERE id=?", (rid,))
    c.commit(); c.close()
    rec_ledger.present(rid, "ask:7", "ask", "queue", db_path=db_path)
    action_queue.snooze(rid, "ask:7", days=1, db_path=db_path, today=date(2026, 9, 23))
    c = get_conn(db_path)
    until = c.execute("SELECT snoozed_until FROM rec_instances WHERE key='ask:7'").fetchone()[0]
    c.close()
    assert until == "2026-09-24 05:00:00"                     # midnight CDT


# ── H-23 iOS reads the queue only after Home has answered ───────────────────

def test_h23_ios_follow_through_waits_for_the_home_fetch():
    ft = open("ios/CavnarAI/CavnarAI/Features/Home/HomeFollowThrough.swift").read()
    assert ".task(id: homeLoadedAt)" in ft and "guard homeLoadedAt != nil" in ft
    assert ".task { await viewModel.load() }" not in ft
    hv = open("ios/CavnarAI/CavnarAI/Features/Home/HomeView.swift").read()
    assert "homeLoadedAt: viewModel.lastLoadedAt" in hv


# ── H-24 a result is in one morning brief ───────────────────────────────────

def test_h24_a_result_lands_in_one_daily_brief_only(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
              "evaluate_on, verdict, status) VALUES (?,?,?,?,?,?,?,?,?)",
              (rid, "recommendation", "trim_day:Monday", "t", "labor_pct", "2026-08-26", "2026-09-22",
               "improved", "evaluated"))
    c.commit(); c.close()
    on_day = outcomes.recent_results(rid, days=1, db_path=db_path, today=date(2026, 9, 22))
    next_day = outcomes.recent_results(rid, days=1, db_path=db_path, today=date(2026, 9, 23))
    assert len(on_day) == 1 and next_day == []


# ── H-26 the gap is to the other days ───────────────────────────────────────

def test_h26_trim_day_measures_against_the_other_days():
    dow = {"Monday": 20.0, "Tuesday": 20.0, "Wednesday": 20.0, "Friday": 40.0}
    assert home_brief.other_days_mean(dow, "Friday") == 20.0
    assert "other_days_mean(dow, worst_day)" in open("home_brief.py").read()


# ── H-27 restoring a kind lifts Home's own hides too ────────────────────────

def test_h27_restore_kind_lifts_home_dismissals_of_that_kind(db_path):
    rid = _rid(db_path)
    home_brief.dismiss(rid, "post_this_week:2026-W39")
    home_brief.dismiss(rid, "post_this_weekend")                   # a different kind, same prefix
    assert "post_this_week:2026-W39" in rec_ledger.silenced_keys(rid)
    decisions.restore_kind(rid, "post_this_week")
    quiet = rec_ledger.silenced_keys(rid)
    assert "post_this_week:2026-W39" not in quiet and "post_this_weekend" in quiet
