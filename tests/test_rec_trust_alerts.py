"""Recommendation-trust audit — the alert side (notify.py, push.py).

Every alert now carries a recommendation identity (rec_ledger), goes only to
the logins allowed to read its module, says the thing once rather than every
morning, and carries what the owner needs to act (dollars, dishes, evidence).
Item numbers are the audit's. Nothing is sent: SMS, email and push are
recorded by stubs, and no model is called.
"""
import sys
from datetime import date, datetime, timedelta

import pytest

import auth
import client_api
import mobile_api
import models
import notify
import ops
import push
import rec_ledger
import webhooks
from auth import create_user, init_auth, set_user_role
from models import Restaurant, create_restaurant, get_conn, save_labor_snapshot, update_restaurant


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, notify, auth, push, client_api, mobile_api, webhooks, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)


@pytest.fixture
def sent(monkeypatch):
    out = {"sms": [], "email": [], "push": []}
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append((to, msg)) or True)
    monkeypatch.setattr(notify, "_send_alert_email",
                        lambda to, subject, html, restaurant_id=None: out["email"].append((subject, html)) or True)
    monkeypatch.setattr(push, "fire_push",
                        lambda rid, at, title, body, data=None, db_path=None, user_ids=None, on_delivered=None:
                        out["push"].append({"rid": rid, "type": at, "body": body, "data": data or {},
                                            "user_ids": user_ids, "on_delivered": on_delivered}))
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return out


def _rid(db_path, **kw):
    kw.setdefault("name", "Trust Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _log(db_path, rid, alert_type):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT id, value FROM alert_log WHERE restaurant_id=? AND alert_type=? ORDER BY id",
                        (rid, alert_type)).fetchall()
    conn.close()
    return rows


def _shown(db_path, rid, key):
    conn = get_conn(db_path)
    rows = [r["surface"] for r in conn.execute(
        "SELECT surface FROM rec_events WHERE restaurant_id=? AND key=? AND event='shown'", (rid, key))]
    conn.close()
    return rows


# ── #6 no_response: a reply owed, said once ──────────────────────────────────

def _negative(db_path, rid, written_days_ago, ext):
    conn = get_conn(db_path)
    written = (datetime.utcnow() - timedelta(days=written_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                 "fetched_at, sentiment, response_status) VALUES (?, 'google', ?, 'G', 1, 'cold', ?, "
                 "datetime('now', '-3 days'), 'negative', 'pending')", (rid, ext, written))
    conn.commit()
    conn.close()


def _no_response_on(db_path, rid):
    update_restaurant(rid, {"alert_no_response": 1, "urgent_via_email": 1}, db_path=db_path)


def test_imported_history_never_raises_the_waiting_alert(db_path, sent):
    """A review written 90 days ago is history, not a reply owed — it used to
    re-alert every day forever."""
    rid = _rid(db_path)
    _no_response_on(db_path, rid)
    _negative(db_path, rid, 90, "old-1")
    _negative(db_path, rid, 200, "old-2")
    notify.check_no_response_alerts(db_path=db_path)
    assert _log(db_path, rid, "no_response") == []


def test_the_waiting_alert_counts_only_recent_reviews_and_repeats_only_when_more_wait(db_path, sent):
    rid = _rid(db_path)
    _no_response_on(db_path, rid)
    _negative(db_path, rid, 5, "new-1")
    _negative(db_path, rid, 400, "ancient")
    notify.check_no_response_alerts(db_path=db_path)
    rows = _log(db_path, rid, "no_response")
    assert len(rows) == 1 and rows[0]["value"] == 1.0, "one recent review, the ancient one not counted"
    # Next morning, same single review: not news.
    conn = get_conn(db_path)
    conn.execute("UPDATE alert_log SET fired_at=datetime('now','-2 days') WHERE restaurant_id=?", (rid,))
    conn.commit(); conn.close()
    notify.check_no_response_alerts(db_path=db_path)
    assert len(_log(db_path, rid, "no_response")) == 1, "the same waiting review is not said again"
    # One more guest waiting: that is news.
    _negative(db_path, rid, 4, "new-2")
    notify.check_no_response_alerts(db_path=db_path)
    assert len(_log(db_path, rid, "no_response")) == 2


def test_the_waiting_email_and_an_all_covered_morning_batch_fold_into_the_brief(db_path, monkeypatch):
    """The alert is raised as "no_response"; the covered set held only
    "unresponded", so it was never folded. A combined batch folds only when
    the brief covers every item in it."""
    # Per recipient since re-audit A-17: the owner's own phone got the brief.
    monkeypatch.setattr(notify, "brief_pushed_emails", lambda *a, **k: {"o@x.test"})
    mailed = []
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: mailed.append(a[1]) or True)
    rid = _rid(db_path)
    assert notify._email_alert(rid, "o@x.test", "s", "<p>x</p>", "no_response") is False
    assert notify._email_alert(rid, "o@x.test", "s", "<p>x</p>", "daily_briefing",
                               covered_types=["critical_low", "no_response"]) is False
    # Labor over target has no line in the brief (A-17): not folded.
    assert notify._email_alert(rid, "o@x.test", "s2", "<p>x</p>", "daily_briefing",
                               covered_types=["labor_over", "no_response"]) is True
    assert mailed == ["s2"]


# ── #34 / #16 labor over target: one threshold, M/D/YY ──────────────────────

def _labor(db_path, rid, pct):
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=6)
    save_labor_snapshot(rid, start.isoformat(), end.isoformat(), pct, pct * 100, 10000, db_path=db_path)
    return start, end


def test_labor_one_point_over_target_is_not_an_alert(db_path, sent):
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_labor_over": 1, "labor_target_pct": 30.0, "urgent_via_email": 1}, db_path=db_path)
    _labor(db_path, rid, 31.0)
    notify.check_daily_alerts(db_path=db_path)
    assert _log(db_path, rid, "labor_over") == []


def test_labor_over_by_the_shared_threshold_alerts_with_an_mdy_period_and_its_own_key(db_path, sent, monkeypatch):
    captured = []
    monkeypatch.setattr(notify, "_alert_email_html",
                        lambda name, headline, lines, **k: captured.extend(lines) or "<p></p>")
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_labor_over": 1, "labor_target_pct": 30.0, "urgent_via_email": 1}, db_path=db_path)
    start, end = _labor(db_path, rid, 34.0)
    notify.check_daily_alerts(db_path=db_path)
    assert len(_log(db_path, rid, "labor_over")) == 1
    period = [l for l in captured if l.startswith("Period:")][0]
    from time_utils import mdy
    assert mdy(start) in period and mdy(end) in period and start.isoformat() not in period
    assert _shown(db_path, rid, f"labor_over:{start.isoformat()}"), "presented under its period key"


# ── #18 an answered recommendation is not re-sent; health always is ──────────

def test_an_alert_the_owner_already_answered_is_not_sent(db_path, sent):
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1}, db_path=db_path)
    rec_ledger.record(rid, "negative_trend", "dismissed", surface="home", db_path=db_path)
    notify.deliver_alert(rid, "negative_trend", "sms", "Rating declining", "<p>x</p>", db_path=db_path)
    assert sent["email"] == [] and sent["push"] == [] and _log(db_path, rid, "negative_trend") == []


def test_a_health_alert_goes_out_whatever_was_answered_before(db_path, sent):
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1}, db_path=db_path)
    rec_ledger.record(rid, "review:77", "dismissed", surface="home", db_path=db_path)
    notify.deliver_alert(rid, "health", "sms", "Health mention", "<p>x</p>", review_id=77, db_path=db_path)
    assert len(_log(db_path, rid, "health")) == 1


def test_a_stock_item_already_answered_is_left_out_of_the_alert(db_path, sent, monkeypatch):
    import inventory
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_food_waste": 1}, db_path=db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda *a, **k: (
        [{"item": "x"}], True, {"critical_low": [{"item": "Salmon"}, {"item": "Lemons"}], "waste_items": []}))
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda *a, **k: ([], False))
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, at, sms, subj, **k: raised.append((at, sms, k.get("recs"))) or True)
    rec_ledger.record(rid, "stock_low:Salmon", "dismissed", surface="home", db_path=db_path)
    notify.check_extra_daily_alerts(db_path=db_path)
    crit = [x for x in raised if x[0] == "critical_low"]
    assert crit and "Lemons" in crit[0][1] and "Salmon" not in crit[0][1]
    assert [r["key"] for r in crit[0][2]] == ["stock_low:Lemons"]


def test_each_channel_that_went_out_is_presented_on_its_own_surface(db_path, sent):
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "urgent_via_sms": 1}, db_path=db_path)
    notify.add_alert_contact(rid, "GM", "+15555550100", sms_consent=True, db_path=db_path)
    notify.deliver_alert(rid, "rating_threshold", "sms", "Below floor", "<p>x</p>", db_path=db_path)
    assert sorted(_shown(db_path, rid, "rating_threshold")) == ["alert_email", "alert_sms"]


# ── #25 the price alert carries the money and the dishes ─────────────────────

def test_the_price_spike_alert_says_the_monthly_exposure_and_the_dishes_it_hits(db_path, monkeypatch):
    import inventory, food_cost_intelligence, menu_intelligence
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_food_waste": 1}, db_path=db_path)
    # A recent applied invoice: the price alert needs one (DH3-4).
    inv_day = (date.today() - timedelta(days=2)).isoformat()
    c = get_conn(db_path)
    c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, applied_at) VALUES (?,?,?,?)",
              (rid, "Sysco", inv_day, inv_day + " 12:00:00"))
    c.commit(); c.close()
    monkeypatch.setattr(inventory, "analysis_for", lambda *a, **k: ([], False, {}))
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda *a, **k: ([{"item": "Salmon"}], True))
    monkeypatch.setattr(inventory, "compute_item_trends", lambda *a, **k: [])
    monkeypatch.setattr(inventory, "build_price_watch", lambda *a, **k: [
        {"item": "Salmon", "is_big_8": True, "change_pct": 12.0, "old_price": 10.0, "new_price": 11.2,
         "kind": "trend"}])
    monkeypatch.setattr(food_cost_intelligence, "cost_drivers", lambda *a, **k: {"drivers": [
        {"kind": "price", "item": "Salmon", "dollars_monthly": 312.0}]})
    monkeypatch.setattr(menu_intelligence, "reprice_suggestions", lambda *a, **k: {"suggestions": [
        {"dish": "Salmon Bowl", "increase_per_plate": 0.84, "monthly_margin_lost": 190.0, "sell_price": 18.0,
         "suggested_price": 19.0, "drivers": [{"ingredient": "Salmon"}]}]})
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, at, sms, subj, **k: raised.append((at, sms, k)) or True)
    notify.check_extra_daily_alerts(db_path=db_path)
    at, sms, k = [x for x in raised if x[0] == "price_spike"][0]
    assert "$312/month" in sms and "Salmon Bowl" in sms
    lines = " ".join(k["lines"])
    assert "$312 a month" in lines and "Salmon Bowl" in lines and "reprice" in lines
    assert k["value"] == 312.0, "alert_log.value is dollars now, not a percentage"
    assert k["recs"][0]["key"] == "price_spike:Salmon" and k["recs"][0]["dollar_value"] == 312.0


# ── #33 an alert reaches only the logins allowed to read it ─────────────────

def test_a_food_alert_is_not_pushed_to_a_manager_who_cannot_see_food_cost(db_path, sent):
    rid = _rid(db_path)
    owner = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    mgr = create_user(rid, "mgr", "mgr@x.test", "pw-mgr-1", db_path=db_path)
    set_user_role(mgr, "manager", db_path=db_path)
    push.register_device_token(owner, rid, "tok-owner", db_path=db_path)
    push.register_device_token(mgr, rid, "tok-mgr", db_path=db_path)
    notify.deliver_alert(rid, "food_waste", "sms", "Waste", "<p>x</p>", db_path=db_path)
    notify.deliver_alert(rid, "labor_over", "sms", "Labor", "<p>x</p>", db_path=db_path)
    by_type = {p["type"]: p["user_ids"] for p in sent["push"]}
    assert by_type["food_waste"] == {owner}, "the manager's role cannot open Food Cost"
    assert by_type["labor_over"] == {owner, mgr}


def test_a_granted_manager_gets_the_food_alert(db_path, sent):
    rid = _rid(db_path)
    mgr = create_user(rid, "mgr", "mgr@x.test", "pw-mgr-1", db_path=db_path)
    set_user_role(mgr, "manager", db_path=db_path)
    push.register_device_token(mgr, rid, "tok-mgr", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO permission_grants (user_id, restaurant_id, permission) VALUES (?,?,?)",
                 (mgr, rid, "foodcost.view"))
    conn.commit(); conn.close()
    assert notify.alert_audience(rid, ["price_spike"], db_path) == {mgr}


# ── #39 the push names the notification it is ───────────────────────────────

def test_the_push_carries_its_alert_log_id_and_the_open_records_it(db_path, sent):
    rid = _rid(db_path)
    uid = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    push.register_device_token(uid, rid, "tok", db_path=db_path)
    notify.deliver_alert(rid, "negative_trend", "sms", "Trend", "<p>x</p>", db_path=db_path)
    data = sent["push"][0]["data"]
    row = _log(db_path, rid, "negative_trend")[0]
    assert data["alert_id"] == row["id"] and data["rec_key"] == "negative_trend"

    from flask import Flask
    app = Flask(__name__)
    inner = mobile_api.mobile_mark_notification_opened.__wrapped__
    user = {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client"}
    with app.test_request_context("/mobile/api/notifications/opened", method="POST",
                                  json={"type": "negative_trend", "alert_id": data["alert_id"],
                                        "rec_key": data["rec_key"]}):
        inner(user)
    conn = get_conn(db_path)
    opened = conn.execute("SELECT alert_log_id, rec_key FROM notification_opens WHERE restaurant_id=?",
                          (rid,)).fetchone()
    ev = conn.execute("SELECT surface FROM rec_events WHERE restaurant_id=? AND key='negative_trend' "
                      "AND event='opened'", (rid,)).fetchall()
    conn.close()
    assert opened["alert_log_id"] == row["id"] and opened["rec_key"] == "negative_trend"
    assert [e["surface"] for e in ev] == ["alert_push"]


def test_an_open_naming_another_restaurants_alert_is_not_linked(db_path):
    a, b = _rid(db_path, name="A"), _rid(db_path, name="B")
    theirs = notify._log_alert(b, "labor_over", db_path=db_path)
    models.record_notification_open(a, "labor_over", user_id=1, db_path=db_path, alert_log_id=theirs)
    conn = get_conn(db_path)
    row = conn.execute("SELECT alert_log_id FROM notification_opens WHERE restaurant_id=?", (a,)).fetchone()
    conn.close()
    assert row["alert_log_id"] is None


# ── #48 competitor movement ──────────────────────────────────────────────────

def _snap(db_path, rid, place, name, rating, count, days_ago):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, review_count, captured_at) "
                 "VALUES (?,?,?,?,?, datetime('now', ?))", (rid, place, name, rating, count, f"-{days_ago} days"))
    conn.commit(); conn.close()


def test_a_competitor_rating_move_alerts_once_with_its_review_counts(db_path, monkeypatch):
    rid = _rid(db_path)
    _snap(db_path, rid, "p1", "Luigi's", 4.6, 300, 8)
    _snap(db_path, rid, "p1", "Luigi's", 4.3, 340, 1)
    _snap(db_path, rid, "p2", "Steady Cafe", 4.5, 90, 8)
    _snap(db_path, rid, "p2", "Steady Cafe", 4.4, 95, 1)
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, at, sms, subj, **k: raised.append((at, sms, k)) or True)
    assert notify.check_competitor_alerts(db_path=db_path)["sent"] == 1
    at, sms, k = raised[0]
    assert at == "competitor_move" and "Luigi's is down 0.3★" in sms and "300 → 340 Google reviews" in sms
    assert "Steady Cafe" not in sms, "a 0.1 move is not news"
    assert [r["key"] for r in k["recs"]] == ["competitor_move:Luigi's"]
    assert notify.check_competitor_alerts(db_path=db_path)["sent"] == 0, "the same move is told once"


def test_the_competitor_alert_respects_its_toggle_and_an_answer(db_path, monkeypatch):
    rid = _rid(db_path)
    _snap(db_path, rid, "p1", "Luigi's", 4.6, 300, 8)
    _snap(db_path, rid, "p1", "Luigi's", 4.2, 330, 1)
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda *a, **k: raised.append(a) or True)
    update_restaurant(rid, {"alert_competitor_move": 0}, db_path=db_path)
    notify.check_competitor_alerts(db_path=db_path)
    assert raised == []
    update_restaurant(rid, {"alert_competitor_move": 1}, db_path=db_path)
    rec_ledger.record(rid, "competitor_move:Luigi's", "dismissed", surface="intel", db_path=db_path)
    notify.check_competitor_alerts(db_path=db_path)
    assert raised == []


def test_the_competitor_alert_toggle_defaults_on():
    assert Restaurant(name="x", owner_email="y").alert_competitor_move == 1
