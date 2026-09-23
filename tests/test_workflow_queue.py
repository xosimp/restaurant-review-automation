"""The action queue and the close-out handoff (workflow audit #7, #8)."""
from datetime import date, datetime, timedelta

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import action_queue, closeout, issues, invoices, menu_intelligence, morning_brief, notify, push, auth
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, action_queue, closeout, issues, invoices, menu_intelligence,
                morning_brief, notify, push, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: True)


def _rid(db_path, **kw):
    kw.setdefault("name", "Queue Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _review(db_path, rid, rating=2, status="drafted"):
    import uuid
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                 "review_date, fetched_at, processed, response_status) "
                 "VALUES (?,'google',?,'A',?,'t',date('now'),datetime('now'),1,?)",
                 (rid, uuid.uuid4().hex[:12], rating, status))
    conn.commit(); conn.close()


# ── the queue ─────────────────────────────────────────────────────────────

def test_the_queue_gathers_what_is_open_across_modules(db_path, monkeypatch):
    import action_queue, issues
    rid = _rid(db_path, module_reviews=1, module_inventory=1)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    issues.create_issue(rid, "manual", "Walk-in is warm", severity="high", db_path=db_path)
    _review(db_path, rid, rating=1)
    models.log_ask_action(rid, "send_guest_campaign", summary="Text the guest club",
                          outcome="proposed", db_path=db_path)

    out = action_queue.items(rid, db_path=db_path)
    kinds = [i["kind"] for i in out["items"]]
    assert kinds[0] == "issue", "an unacknowledged high issue leads"
    assert "reviews" in kinds and "proposal" in kinds
    assert all(i["action"] for i in out["items"]), "every item says what finishes it"


def test_snoozing_puts_an_item_back_tomorrow_not_away(db_path):
    import action_queue
    rid = _rid(db_path, module_reviews=1)
    _review(db_path, rid)
    today = date(2026, 9, 21)
    assert [i["key"] for i in action_queue.items(rid, db_path=db_path, today=today)["items"]] \
        == ["reviews_waiting"]
    action_queue.snooze(rid, "reviews_waiting", db_path=db_path, today=today)
    assert action_queue.items(rid, db_path=db_path, today=today)["items"] == []
    tomorrow = today + timedelta(days=1)
    assert [i["key"] for i in action_queue.items(rid, db_path=db_path, today=tomorrow)["items"]] \
        == ["reviews_waiting"], "still open tomorrow"


def test_a_snooze_is_bounded(db_path):
    import action_queue
    rid = _rid(db_path)
    out = action_queue.snooze(rid, "x", days=365, db_path=db_path, today=date(2026, 9, 21))
    assert out["until"] == "2026-10-05", "at most two weeks"


def test_a_manager_without_food_cost_gets_no_food_cost_tasks(db_path, monkeypatch):
    import action_queue, invoices, menu_intelligence
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(invoices, "list_imports", lambda *a, **k: [{"id": 1, "applied_at": None}])
    monkeypatch.setattr(menu_intelligence, "reprice_suggestions", lambda *a, **k: {"suggestions": [
        {"dish": "Parm", "monthly_margin_lost": 140}]})
    owner_keys = [i["key"] for i in action_queue.items(rid, db_path=db_path)["items"]]
    assert {"invoice:pending", "reprice:Parm"} <= set(owner_keys)
    mgr = {"id": 5, "role": "manager", "is_admin": 0, "grants": frozenset()}
    mgr_keys = [i["key"] for i in action_queue.items(rid, viewer=mgr, db_path=db_path)["items"]]
    assert not {"invoice:pending", "reprice:Parm"} & set(mgr_keys)


def test_next_weeks_schedule_only_nags_from_thursday(db_path):
    import action_queue
    rid = _rid(db_path, module_labor=1)
    keys = lambda d: [i["key"] for i in action_queue.items(rid, db_path=db_path, today=d)["items"]]
    assert "schedule:next-week" in keys(date(2026, 9, 24))      # Thursday
    assert "schedule:next-week" not in keys(date(2026, 9, 22))  # Tuesday


# ── the close-out ─────────────────────────────────────────────────────────

def test_a_close_out_filed_after_midnight_belongs_to_the_night_before(db_path):
    import closeout
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    assert closeout.business_date_for(r, now_local=datetime(2026, 9, 22, 1, 10)) == date(2026, 9, 21)
    assert closeout.business_date_for(r, now_local=datetime(2026, 9, 21, 23, 40)) == date(2026, 9, 21)


def test_re_filing_replaces_rather_than_duplicates(db_path):
    import closeout
    rid = _rid(db_path)
    closeout.save(rid, {"went_wrong": "Ticket times"}, submitted_by="Sam",
                  business_date="2026-09-21", db_path=db_path)
    closeout.save(rid, {"went_wrong": "Ticket times over 20 min"}, submitted_by="Sam",
                  business_date="2026-09-21", db_path=db_path)
    conn = get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM close_outs WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
    conn.close()
    assert "over 20 min" in closeout.get(rid, "2026-09-21")["went_wrong"]


def test_an_empty_close_out_is_refused(db_path):
    import closeout
    with pytest.raises(ValueError):
        closeout.save(_rid(db_path), {"went_wrong": "   "}, business_date="2026-09-21", db_path=db_path)


def test_last_nights_close_out_leads_this_mornings_brief(db_path):
    import closeout, morning_brief
    rid = _rid(db_path)
    closeout.save(rid, {"went_wrong": "Walk-in door left open", "eighty_sixed": "salmon",
                        "callouts": "Dana"}, submitted_by="Sam",
                  business_date="2026-09-20", db_path=db_path)
    lines = {l["key"]: l for l in morning_brief.build(rid, today=date(2026, 9, 21),
                                                      db_path=db_path)["lines"]}
    assert "closeout" in lines
    text = lines["closeout"]["text"]
    assert "Sam at close" in text and "Walk-in door" in text and "86'd: salmon" in text


def test_a_stale_close_out_is_not_todays_news(db_path):
    import closeout, morning_brief
    rid = _rid(db_path)
    closeout.save(rid, {"went_wrong": "old news"}, business_date="2026-09-01", db_path=db_path)
    lines = {l["key"] for l in morning_brief.build(rid, today=date(2026, 9, 21),
                                                   db_path=db_path)["lines"]}
    assert "closeout" not in lines


# ── the lineup nudge (workflow audit #11) ─────────────────────────────────

def test_the_lineup_nudge_texts_the_routed_manager_not_staff(db_path, monkeypatch):
    """Staff phone numbers carry no SMS consent; the routed manager's does."""
    import issues, preshift, strategy_jobs, time_utils, auth
    auth.init_auth(db_path=db_path)
    rid = _rid(db_path)
    models.update_restaurant(rid, {"preshift_nudge_hour": 16}, db_path=db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    monkeypatch.setattr(preshift, "build", lambda *a, **k: {"items": [
        {"kind": "watch", "text": "Watch service speed tonight."}]})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 16, 10))
    sent = []
    monkeypatch.setattr("notify.send_sms", lambda to, msg, use_case="alert": sent.append((to, msg)) or True)
    assert strategy_jobs.run_preshift_nudge(db_path=db_path)["sent"] == 1
    assert sent[0][0] == "+15555550100" and "lineup notes" in sent[0][1] and "/staff/r/" in sent[0][1]
    assert strategy_jobs.run_preshift_nudge(db_path=db_path)["sent"] == 0, "once a day"


def test_the_nudge_is_off_by_default_and_silent_with_nothing_to_say(db_path, monkeypatch):
    import issues, preshift, strategy_jobs, time_utils, auth
    auth.init_auth(db_path=db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 16, 10))
    sent = []
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: sent.append(a) or True)
    assert strategy_jobs.run_preshift_nudge(db_path=db_path)["sent"] == 0, "off by default"

    models.update_restaurant(rid, {"preshift_nudge_hour": 16}, db_path=db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    monkeypatch.setattr(preshift, "build", lambda *a, **k: {"items": []})
    assert strategy_jobs.run_preshift_nudge(db_path=db_path)["sent"] == 0, "nothing to say"
    assert sent == []


def test_every_queue_action_works_on_both_clients(db_path):
    """The web and mobile APIs have different prefixes — an action carrying
    one path 404s on the other client."""
    import action_queue, issues
    rid = _rid(db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    issues.create_issue(rid, "manual", "Walk-in is warm", db_path=db_path)
    for item in action_queue.items(rid, db_path=db_path)["items"]:
        route = (item["action"] or {}).get("route")
        if route is None:
            assert item["action"].get("module"), "an action must route somewhere"
            continue
        assert route["web"].startswith("/api/")
        assert route["mobile"] == "/mobile" + route["web"]


def test_a_proposal_the_owner_already_answered_leaves_the_queue(db_path):
    """Confirming writes its own row rather than updating the proposal."""
    import action_queue
    rid = _rid(db_path)
    models.log_ask_action(rid, "send_supplier_order", summary="Order from Fresh Co",
                          outcome="proposed", db_path=db_path)
    keys = lambda: [i["key"] for i in action_queue.items(rid, db_path=db_path)["items"]]
    assert "proposal:send_supplier_order:Order from Fresh Co" in keys()
    models.log_ask_action(rid, "send_supplier_order", summary="Order from Fresh Co",
                          outcome="confirmed", db_path=db_path)
    assert "proposal:send_supplier_order:Order from Fresh Co" not in keys()


# ── recommendation trust audit (#17, #18, #40) ────────────────────────────

def test_a_snooze_is_also_a_ledger_event_with_its_until(db_path):
    """#40: action_snoozes keeps only the latest snooze; the ledger keeps
    every one, with when it ends, and the key is quiet everywhere till then."""
    import action_queue, rec_ledger
    rid = _rid(db_path, module_reviews=1)
    _review(db_path, rid)
    action_queue.items(rid, db_path=db_path)
    action_queue.snooze(rid, "reviews_waiting", days=3, db_path=db_path, today=date.today())
    action_queue.snooze(rid, "reviews_waiting", days=5, db_path=db_path, today=date.today())
    conn = get_conn(db_path)
    rows = conn.execute("SELECT meta FROM rec_events WHERE restaurant_id=? AND key='reviews_waiting' "
                        "AND event='snoozed' ORDER BY id", (rid,)).fetchall()
    conn.close()
    import json
    assert [json.loads(r["meta"])["days"] for r in rows] == [3, 5]
    assert rec_ledger.silenced(rid, "reviews_waiting", db_path=db_path)


def test_two_proposals_of_one_kind_are_two_items(db_path):
    """#40: `proposal:{action}` hid every proposal of that kind at once."""
    import action_queue
    rid = _rid(db_path)
    models.log_ask_action(rid, "send_supplier_order", summary="Order from Fresh Co",
                          outcome="proposed", db_path=db_path)
    models.log_ask_action(rid, "send_supplier_order", summary="Order from Sysco",
                          outcome="proposed", db_path=db_path)
    action_queue.snooze(rid, "proposal:send_supplier_order:Order from Fresh Co", db_path=db_path)
    keys = [i["key"] for i in action_queue.items(rid, db_path=db_path)["items"]]
    assert keys == ["proposal:send_supplier_order:Order from Sysco"]


def test_each_dish_to_reprice_is_its_own_item_with_one_tap_apply(db_path, monkeypatch):
    import action_queue, menu_intelligence
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(menu_intelligence, "reprice_suggestions", lambda *a, **k: {"suggestions": [
        {"dish": "Parm", "monthly_margin_lost": 140, "suggested_price": 21.5},
        {"dish": "Carbonara", "monthly_margin_lost": 60, "suggested_price": 18.25}]})
    items = {i["key"]: i for i in action_queue.items(rid, db_path=db_path)["items"]}
    assert {"reprice:Parm", "reprice:Carbonara"} <= set(items)
    act = items["reprice:Parm"]["action"]
    assert act["route"]["web"] == "/api/food-cost/reprice/apply"
    assert act["route"]["mobile"] == "/mobile" + act["route"]["web"]
    assert act["body"] == {"dish": "Parm", "price": 21.5}


def test_an_answer_on_home_silences_the_same_key_in_the_queue(db_path):
    """#17: one "no" everywhere."""
    import action_queue, rec_ledger
    rid = _rid(db_path, module_labor=1)
    thursday = date(2026, 9, 24)
    assert "schedule:next-week" in [i["key"] for i in action_queue.items(rid, db_path=db_path, today=thursday)["items"]]
    rec_ledger.record(rid, "schedule:next-week", "dismissed", surface="home", meta={"kind": "hide"}, db_path=db_path)
    assert "schedule:next-week" not in [i["key"] for i in action_queue.items(rid, db_path=db_path, today=thursday)["items"]]


def test_news_home_already_said_today_is_not_repeated_unless_critical(db_path):
    """#18: the same news once a day across Home, the brief and the queue."""
    import action_queue, rec_ledger
    rid = _rid(db_path, module_labor=1, module_reviews=1)
    thursday = date(2026, 9, 24)
    _review(db_path, rid, rating=1)
    rec_ledger.present_many(rid, [{"key": "schedule:next-week", "module": "labor"},
                                  {"key": "reviews_waiting", "module": "reviews"}], "home", db_path=db_path)
    out = action_queue.items(rid, db_path=db_path, today=thursday)
    keys = [i["key"] for i in out["items"]]
    assert "schedule:next-week" not in keys and out["shown_elsewhere"] == 1
    assert "reviews_waiting" in keys, "a 1-star guest waiting is critical and is never deduped"
