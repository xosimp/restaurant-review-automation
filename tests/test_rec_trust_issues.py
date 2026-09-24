"""Recommendation-trust audit — issues, coverage, the scheduled nudges and
Ask proposals (issues.py, intraday.py, strategy_jobs.py, ask_cavnar*.py).

Item numbers are the audit's. Nothing is sent and no model is called: SMS,
email and push are stubs.
"""
import json
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import models
import rec_ledger
from models import Restaurant, create_restaurant, get_conn, update_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import intraday, issues, notify, push, strategy_jobs, ops, client_api, mobile_api, ask_cavnar
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, intraday, issues, notify, push, auth, strategy_jobs, ops, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(ask_cavnar, "invalidate_context", lambda *a, **k: None)


@pytest.fixture
def texts(monkeypatch):
    out = []
    monkeypatch.setattr("notify.send_sms", lambda to, msg, use_case="alert": out.append((to, msg)) or True)
    return out


def _rid(db_path, **kw):
    kw.setdefault("name", "Floor Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _contact(db_path, rid, name="GM", phone="+15555550100", consent=1):
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,?)",
                       (rid, name, phone, consent)).lastrowid
    conn.commit(); conn.close()
    return cid


def _routed(db_path, rid):
    import issues
    cid = _contact(db_path, rid)
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    return cid


# ── #1 an issue filed without a text stays untexted ──────────────────────────

def test_an_issue_filed_with_notify_false_is_never_texted_by_the_tick(db_path, texts):
    """Reproduces the audit: a comp/void flag naming a manager is filed with
    notify=False, and the next tick found it open, assigned and un-notified —
    and texted the routed manager anyway."""
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    issue, _tok = issues.create_issue(rid, "loss", "Comps concentrated on one manager",
                                      source_key="loss:2026-W38:comp:17", notify=False, db_path=db_path)
    assert texts == []
    for _ in range(3):
        issues.tick(db_path=db_path)
    assert texts == [], "tick texted an issue deliberately filed without a text"
    # It is still an issue the owner can see.
    assert [i["id"] for i in issues.list_issues(rid, status="unresolved", db_path=db_path)] == [issue["id"]]


def test_a_normal_issue_held_by_quiet_hours_is_still_sent_by_the_tick(db_path, texts, monkeypatch):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: True)
    issues.create_issue(rid, "manual", "Walk-in is warm", db_path=db_path)
    assert texts == []
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: False)
    issues.tick(db_path=db_path)
    assert len(texts) == 1


def test_reassigning_a_filed_issue_by_name_does_text_the_new_assignee(db_path, texts):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    other = _contact(db_path, rid, name="Owner2", phone="+15555550199")
    issue, _ = issues.create_issue(rid, "plan", "Trim Tuesday", notify=False, db_path=db_path)
    issues.reassign(rid, issue["id"], other, db_path=db_path)
    assert [t[0] for t in texts] == ["+15555550199"]


# ── #34 the labor issue can open, on the shared threshold ────────────────────

def test_the_labor_issue_reads_overall_labor_pct_and_the_shared_threshold(db_path, texts, monkeypatch):
    import issues, labor
    rid = _rid(db_path)
    update_restaurant(rid, {"labor_target_pct": 30.0}, db_path=db_path)
    _routed(db_path, rid)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": True,
                                                                                  "overall_labor_pct": 32.0})
    assert issues.open_from_signals(rid, db_path=db_path) == [], "2 points over is not over"
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": True,
                                                                                  "overall_labor_pct": 33.5})
    opened = issues.open_from_signals(rid, db_path=db_path)
    assert [i["kind"] for i in opened] == ["labor"] and "33.5%" in opened[0]["title"]


# ── #18 one stock issue, kept current, closed when it clears ─────────────────

def _stock(monkeypatch, low):
    import inventory
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda *a, **k: ([{"item": "x"}], True))
    monkeypatch.setattr(inventory, "analysis_for", lambda *a, **k: (None, None, {
        "critical_low": [{"item": n} for n in low]}))


def test_low_stock_is_one_open_issue_updated_daily_and_closed_when_back_above_par(db_path, texts, monkeypatch):
    import issues
    rid = _rid(db_path, module_labor=0, module_inventory=1)
    _routed(db_path, rid)
    _stock(monkeypatch, ["Salmon"])
    assert len(issues.open_from_signals(rid, db_path=db_path, today=date(2026, 9, 21))) == 1
    _stock(monkeypatch, ["Salmon", "Lemons"])
    assert issues.open_from_signals(rid, db_path=db_path, today=date(2026, 9, 22)) == []
    stock = [i for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "stock"]
    assert len(stock) == 1 and "Lemons" in stock[0]["detail"] and stock[0]["meta"]["items"] == ["Salmon", "Lemons"]
    assert len(texts) == 1, "the manager is texted once, not every morning"
    _stock(monkeypatch, [])
    issues.open_from_signals(rid, db_path=db_path, today=date(2026, 9, 23))
    stock = [i for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "stock"]
    assert stock[0]["status"] == "resolved" and "above par" in stock[0]["resolution_note"]


def test_a_review_issue_closes_itself_when_its_reply_posts(db_path, texts):
    import issues
    rid = _rid(db_path)
    _routed(db_path, rid)
    conn = get_conn(db_path)
    rev = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                       "review_date, fetched_at, response_status) VALUES (?, 'google', 'r1', 'Al', 1, 'bad', "
                       "datetime('now'), datetime('now'), 'drafted')", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.open_from_reviews(rid, db_path=db_path)
    assert issues.auto_close(rid, db_path=db_path) == 0
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET response_status='posted' WHERE id=?", (rev,))
    conn.commit(); conn.close()
    assert issues.auto_close(rid, db_path=db_path) == 1
    closed = issues.list_issues(rid, db_path=db_path)[0]
    assert closed["status"] == "resolved"
    assert f"review:{rev}" in rec_ledger.silenced_keys(rid, db_path=db_path), "resolution answers the recommendation"


# ── #13 coverage reads the published week, matches people, closes itself ────

def _week(db_path, rid, day, rows, published=True, superseded_by=None):
    from models import save_schedule_history
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(
        f"{day.isoformat()},{day.strftime('%A')},{e},{r},{s},{end},6," for e, r, s, end in rows)
    hid = save_schedule_history(rid, day.isoformat(), day.isoformat(), 40, 40, 30, csv, [], db_path=db_path)
    conn = get_conn(db_path)
    if published:
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    if superseded_by:
        conn.execute("UPDATE schedule_history SET superseded_by=? WHERE id=?", (superseded_by, hid))
    conn.commit(); conn.close()
    return hid


DAY = date(2026, 9, 21)


def test_a_draft_nobody_published_is_not_a_schedule_to_be_late_for(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    _week(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "5:00pm")], published=False)
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda *a: ([], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 12, 0), db_path=db_path)
    assert out["available"] is False and "published" in out["reason"]


def test_names_match_through_name_key_abbreviations_and_pos_ids(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    _week(db_path, rid, DAY, [("Dana  K", "Server", "11:00am", "5:00pm"), ("Maria G.", "Cook", "10:00am", "4:00pm"),
                              ("Sam Park", "Host", "10:00am", "4:00pm"), ("Lee Wu", "Bar", "10:00am", "4:00pm")])
    conn = get_conn(db_path)
    conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, pos_id) VALUES (?, 'Sam Park', 'E-77')",
                 (rid,))
    conn.commit(); conn.close()
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda *a: ([
        {"employee": "dana k"}, {"employee": "Maria Garcia"}, {"employee": "S. Park", "pos_id": "E-77"}], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 12, 0), db_path=db_path)
    assert [m["employee"] for m in out["missing"]] == ["Lee Wu"]


def test_two_people_sharing_an_abbreviation_is_not_a_match(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    _week(db_path, rid, DAY, [("Maria G.", "Cook", "10:00am", "4:00pm"), ("Maria Gomez", "Cook", "10:00am", "4:00pm")])
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda *a: ([{"employee": "Maria Garcia"}], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 12, 0), db_path=db_path)
    assert sorted(m["employee"] for m in out["missing"]) == ["Maria G.", "Maria Gomez"]


def _coverage_world(db_path, monkeypatch, clocked):
    import intraday, pos, time_utils, labor_replacements
    rid = _rid(db_path)
    update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}', "close_times_json": '{"Monday": "10:00pm"}'},
                      db_path=db_path)
    _routed(db_path, rid)
    _week(db_path, rid, DAY, [("Dana K", "Server", "11:00am", "5:00pm")])
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 11, 40))
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda *a: (clocked(), "toast"))
    monkeypatch.setattr(labor_replacements, "for_gap", lambda *a, **k: [{"name": "Ana", "score": 4.0}])
    return rid


def test_a_late_arrival_closes_their_own_no_show_issue(db_path, texts, monkeypatch):
    import issues, strategy_jobs
    here = []
    rid = _coverage_world(db_path, monkeypatch, lambda: list(here))
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    here.append({"employee": "Dana K"})
    strategy_jobs.run_coverage_check(db_path=db_path)
    cov = issues.list_issues(rid, db_path=db_path)[0]
    assert cov["status"] == "resolved" and "clocked in" in cov["resolution_note"]


def test_ask_ana_to_cover_texts_her_consented_number_and_records_the_yes(db_path, texts, monkeypatch):
    import issues, intraday, strategy_jobs
    rid = _coverage_world(db_path, monkeypatch, lambda: [])
    strategy_jobs.run_coverage_check(db_path=db_path)
    cov = issues.list_issues(rid, db_path=db_path)[0]
    assert cov["meta"]["covers"] == [{"name": "Ana", "score": 4.0}]
    conn = get_conn(db_path)
    conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, phone) VALUES (?, 'Ana', '(555) 555-0142')",
                 (rid,))
    conn.commit(); conn.close()
    # No consent on record for her number: never texted.
    out = intraday.ask_to_cover(rid, cov["id"], "Ana", db_path=db_path)
    assert out["ok"] is False and "call them" in out["error"]
    _contact(db_path, rid, name="Ana", phone="+15555550142")
    texts.clear()
    out = intraday.ask_to_cover(rid, cov["id"], "ana", user_id=9, db_path=db_path)
    assert out == {"ok": True, "via": "sms", "name": "Ana"}
    assert len(texts) == 1 and "Dana K" in texts[0][1] and "cover" in texts[0][1]
    conn = get_conn(db_path)
    ev = conn.execute("SELECT event FROM rec_events WHERE restaurant_id=? AND key=?",
                      (rid, intraday.cover_key(cov))).fetchall()
    conn.close()
    assert "accepted" in [e["event"] for e in ev]
    assert intraday.ask_to_cover(rid, cov["id"], "Ana", db_path=db_path)["ok"] is False, "asked once"
    assert intraday.ask_to_cover(rid, cov["id"], "Somebody Else", db_path=db_path)["ok"] is False


def test_the_issue_page_offers_the_one_tap_and_sends_it(db_path, texts, monkeypatch):
    import issues, strategy_jobs
    from strategy_routes import issue_link_bp
    rid = _coverage_world(db_path, monkeypatch, lambda: [])
    _contact(db_path, rid, name="Ana", phone="+15555550142")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, phone) VALUES (?, 'Ana', '+15555550142')",
                 (rid,))
    conn.commit(); conn.close()
    strategy_jobs.run_coverage_check(db_path=db_path)
    cov = issues.list_issues(rid, db_path=db_path)[0]
    token = issues._mint_link(cov["id"], cov["assignee_contact_id"], db_path=db_path)
    app = Flask(__name__, template_folder="../templates")
    app.add_template_filter(lambda d: "9/21/26", "format_date")
    app.register_blueprint(issue_link_bp)
    client = app.test_client()
    page = client.get(f"/i/{token}").data.decode()
    assert "Ask Ana to cover" in page and "2026-" not in page
    texts.clear()
    page = client.post(f"/i/{token}", data={"action": "ask_cover", "name": "Ana"}).data.decode()
    assert "Asked Ana by text" in page and len(texts) == 1


# ── #16 the trusted-order subject is in the restaurant's own time ────────────

def test_the_trusted_order_time_is_local_not_utc(db_path):
    import strategy_jobs
    r = Restaurant(name="x", owner_email="y", timezone="America/Chicago")
    assert strategy_jobs._local_clock(r, "2026-09-21 14:00:00") == "9am"
    assert strategy_jobs._local_clock(r, "2026-09-21 18:30:00") == "1:30pm"


# ── #32 the quiet-night push: the date first, then the week ──────────────────

def test_a_monday_with_no_quiet_night_does_not_spend_the_week(db_path, monkeypatch):
    import demand, strategy_jobs, push, morning_brief, time_utils, ops
    rid = _rid(db_path, module_marketing=1)
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append(a[1]))
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 1}])
    # A push-only heads-up goes to someone whose phone can take it.
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1}])
    monkeypatch.setattr(strategy_jobs, "_draft_quiet_night_fill", lambda *a, **k: {})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 10, 30))
    monkeypatch.setattr(demand, "quiet_night_ahead", lambda *a, **k: {"available": False, "reason": "Wednesday is fine"})
    strategy_jobs.run_demand_opportunity(db_path=db_path)
    assert fired == [] and not ops.period_claimed(f"demand_opportunity:{rid}", "2026-W39")
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 22, 10, 30))
    monkeypatch.setattr(demand, "quiet_night_ahead", lambda *a, **k: {
        "available": True, "date": "2026-09-24", "weekday": "Thursday", "typical_sales": 2100.0,
        "samples": 6, "below_average_pct": 31.0})
    assert strategy_jobs.run_demand_opportunity(db_path=db_path)["sent"] == 1
    assert strategy_jobs.run_demand_opportunity(db_path=db_path)["sent"] == 0, "still once a week"


def _qn_week(db_path, rid, days_ago, status="draft"):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?, 'demand_opportunity', "
                 "datetime('now', ?))", (rid, f"-{days_ago} days"))
    conn.execute("INSERT INTO marketing_drafts (restaurant_id, content_type, topic, body, status, created_at) "
                 "VALUES (?, 'instagram_post', 'Tuesday night — a reason to come in this week', 'x', ?, "
                 "datetime('now', ?))", (rid, status, f"-{days_ago} days"))
    conn.commit(); conn.close()


def test_the_run_stops_drafting_after_two_ignored_weeks(db_path, monkeypatch):
    import demand, strategy_jobs, push, morning_brief, time_utils
    rid = _rid(db_path, module_marketing=1)
    _qn_week(db_path, rid, 14)
    _qn_week(db_path, rid, 7)
    drafted = []
    monkeypatch.setattr(strategy_jobs, "_draft_quiet_night_fill", lambda *a, **k: drafted.append(1) or {})
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: None)
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1}])
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime.utcnow().replace(hour=10))
    monkeypatch.setattr(demand, "quiet_night_ahead", lambda *a, **k: {
        "available": True, "date": "2026-09-24", "weekday": "Thursday", "typical_sales": 2100.0,
        "samples": 6, "below_average_pct": 31.0})
    assert strategy_jobs.run_demand_opportunity(db_path=db_path)["sent"] == 1
    assert drafted == [], "two weeks of unapproved drafts: the heads-up goes, the drafting does not"


def test_two_ignored_weeks_stop_the_drafting_and_stale_drafts_expire(db_path):
    import strategy_jobs
    rid = _rid(db_path, module_marketing=1)
    _qn_week(db_path, rid, 14)
    assert strategy_jobs.quiet_night_ignored_weeks(rid, db_path=db_path) == 1
    _qn_week(db_path, rid, 7)
    assert strategy_jobs.quiet_night_ignored_weeks(rid, db_path=db_path) == 2
    assert strategy_jobs.expire_quiet_night_drafts(rid, db_path=db_path) == 2
    conn = get_conn(db_path)
    assert {r["status"] for r in conn.execute("SELECT status FROM marketing_drafts")} == {"expired"}
    conn.close()
    _qn_week(db_path, rid, 0, status="approved")
    assert strategy_jobs.quiet_night_ignored_weeks(rid, db_path=db_path) == 0, "an approval resets it"


# ── #50 the pulse names one staffing move ────────────────────────────────────

def test_a_slow_night_with_spare_servers_names_the_latest_starter_and_the_saving(db_path):
    import strategy_jobs
    rid = _rid(db_path)
    # "Never cut below" one: this is about who is named and what it saves;
    # the default of two keeps both servers on (test_cut_floor_default.py).
    update_restaurant(rid, {"role_rates_json": json.dumps({"Server": 15.0}), "cut_floor_default": 1},
                      db_path=db_path)
    _week(db_path, rid, DAY, [("Ana", "Server", "4:00pm", "10:00pm"), ("Bo", "Server", "5:00pm", "11:00pm"),
                              ("Cy", "Server", "11:00am", "5:00pm"), ("Di", "Cook", "4:00pm", "11:00pm")])
    r = models.get_restaurant(rid, db_path)
    local = datetime(2026, 9, 21, 16, 30)
    move = strategy_jobs.staffing_move(r, local, {"direction": "behind", "pct": -22.0, "samples": 6}, db_path=db_path)
    assert move["employee"] == "Bo" and move["hours"] == 3.0 and move["dollars"] == 45
    assert "go at 8pm" in move["text"] and "2 servers on" in move["text"]
    assert strategy_jobs.staffing_move(r, local, {"direction": "behind", "pct": -10.0, "samples": 6},
                                       db_path=db_path) is None
    # Three past readings are enough to say the night is behind, not to cut a
    # shift on (intraday.MIN_SAMPLES_FOR_CUT; CA1 L28, fix I13).
    assert strategy_jobs.staffing_move(r, local, {"direction": "behind", "pct": -22.0, "samples": 3},
                                       db_path=db_path) is None
    update_restaurant(rid, {"role_floors_json": json.dumps({"Server": {"night": 2}})}, db_path=db_path)
    r = models.get_restaurant(rid, db_path)
    assert strategy_jobs.staffing_move(r, local, {"direction": "behind", "pct": -22.0, "samples": 6},
                                       db_path=db_path) is None, \
        "at the floor there is nobody to spare"


# ── #49 review requests: measured, and nudged ────────────────────────────────

def test_review_request_conversion_matches_by_name_inside_14_days(db_path):
    import strategy_jobs
    rid = _rid(db_path)
    conn = get_conn(db_path)
    for i in range(6):
        conn.execute("INSERT INTO review_requests (restaurant_id, customer_name, customer_email, sent_at) "
                     "VALUES (?, ?, 'g@x.test', datetime('now', '-30 days'))", (rid, f"Guest {i}"))
    conn.execute("INSERT INTO review_requests (restaurant_id, customer_name, customer_email, sent_at) "
                 "VALUES (?, 'Too New', 'g@x.test', datetime('now', '-3 days'))", (rid,))
    for ext, author, ago in (("a", "guest 0", 25), ("b", "Guest 1", 5), ("c", "Stranger", 25)):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at) VALUES (?, 'google', ?, ?, 5, 'great', datetime('now', ?), datetime('now'))",
                     (rid, ext, author, f"-{ago} days"))
    conn.commit(); conn.close()
    out = strategy_jobs.review_request_conversion(rid, db_path=db_path)
    assert out["measured"] == 6, "a request whose 14 days haven't closed is not measured"
    assert out["matched"] == 1, "Guest 1 reviewed 25 days after being asked — outside the window"
    assert out["rate"] == round(1 / 6, 3) and out["basis"] == "name" and "name only" in out["caveat"]


def test_the_weekly_nudge_goes_when_requests_are_off_with_the_measured_rate(db_path, monkeypatch):
    import strategy_jobs
    rid = _rid(db_path, module_reviews=1)
    reached = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, at, title, body, data, db, **k:
                        reached.append((at, title, body, k.get("rec"))) or 1)
    monkeypatch.setattr("scheduler._fetch_order", lambda ids, key=None: sorted(ids))
    monkeypatch.setattr("scheduler._remember_fetch_cursor", lambda *a, **k: None)
    monday = datetime(2026, 9, 21, 10, 15)
    assert strategy_jobs.run_review_request_nudge(db_path=db_path, now_local=monday)["sent"] == 1
    at, title, body, rec = reached[0]
    assert at == "review_request_nudge" and "No review requests" in title and rec["key"] == "review_requests"
    assert strategy_jobs.run_review_request_nudge(db_path=db_path, now_local=monday)["sent"] == 0, "weekly"
    tuesday = datetime(2026, 9, 29, 10, 15)
    assert strategy_jobs.run_review_request_nudge(db_path=db_path, now_local=tuesday)["sent"] == 0


# ── #23 Ask proposals: one identity, the content, the reason ─────────────────

def test_proposals_are_logged_with_their_own_id_and_answered_by_it(db_path):
    import ask_cavnar, client_api
    rid = _rid(db_path)
    props = [{"action": "send_supplier_order", "summary": "Email the order", "body": {}},
             {"action": "send_supplier_order", "summary": "Email the order", "body": {}}]
    ask_cavnar.record_proposals(rid, props, user_id=4)
    a, b = props[0]["proposal_id"], props[1]["proposal_id"]
    assert a and b and a != b
    payload, status = client_api._do_record_ask_action(rid, 4, {
        "action": "send_supplier_order", "outcome": "dismissed", "summary": "Email the order",
        "proposal_id": a, "reason": "  Supplier is closed this week "})
    assert status == 200
    settled = models.get_ask_proposal(rid, a)["settled"]
    assert settled["outcome"] == "dismissed" and settled["reason"] == "Supplier is closed this week"
    assert models.get_ask_proposal(rid, b)["settled"] is None, "the other proposal is still open"
    assert ask_cavnar.proposal_key(a) in rec_ledger.silenced_keys(rid, db_path=db_path)
    assert ask_cavnar.proposal_key(b) not in rec_ledger.silenced_keys(rid, db_path=db_path)


def test_an_answer_naming_another_restaurants_proposal_is_refused(db_path):
    import ask_cavnar, client_api
    mine, theirs = _rid(db_path, name="Mine"), _rid(db_path, name="Theirs")
    props = [{"action": "publish_schedule", "summary": "Send it", "body": {}}]
    ask_cavnar.record_proposals(theirs, props)
    payload, status = client_api._do_record_ask_action(mine, 1, {
        "action": "publish_schedule", "outcome": "confirmed", "proposal_id": props[0]["proposal_id"]})
    assert status == 404


def test_a_supplier_order_card_shows_the_total_and_who_it_goes_to(db_path, monkeypatch):
    import ask_cavnar_tools, inventory
    rid = _rid(db_path)
    monkeypatch.setattr(inventory, "build_supplier_orders", lambda *a, **k: {
        "is_live": True, "draft_hash": "h1", "groups": [
            {"supplier_name": "Sysco", "supplier_email": "orders@sysco.test", "total_cost": 412.5,
             "items": [{}, {}, {}]},
            {"supplier_name": "Farm", "supplier_email": "farm@x.test", "total_cost": 88.0, "items": [{}]}]})
    p = ask_cavnar_tools.build_proposal("send_supplier_order", {}, restaurant_id=rid)
    assert p["at_stake"] == 500.5
    assert {"label": "Order total", "value": "$500.50"} in p["details"]
    assert any("orders@sysco.test" in d["value"] and "3 items" in d["value"] for d in p["details"])


def test_an_approve_review_card_shows_the_reply_that_would_post(db_path):
    import ask_cavnar_tools
    rid = _rid(db_path)
    conn = get_conn(db_path)
    rev = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                       "fetched_at, draft_response, response_status) VALUES (?, 'google', 'x1', 'Al', 2, "
                       "'Slow service', datetime('now'), 'Sorry Al — we will do better.', 'drafted')",
                       (rid,)).lastrowid
    conn.commit(); conn.close()
    p = ask_cavnar_tools.build_proposal("approve_review", {"review_id": rev}, restaurant_id=rid)
    assert p["preview"] == "Sorry Al — we will do better."
    assert any("Slow service" in d["value"] for d in p["details"])


def test_decisions_leave_loss_issues_out_for_a_login_without_loss_view(monkeypatch):
    """Loss issues name the manager who approved the comps (re-audit A-8):
    the /decisions route, Ask's decisions context and its read_decisions tool
    all pass the viewer's LOSS_VIEW through to decisions.history."""
    import ask_cavnar, ask_cavnar_tools, decisions, strategy_routes, issues as _iss
    seen = []
    monkeypatch.setattr(decisions, "history", lambda rid, limit=40, db_path=None, sees_loss=True, viewer=None: seen.append(("h", sees_loss)) or [])
    monkeypatch.setattr(decisions, "context", lambda rid, db_path=None, sees_loss=True, viewer=None: seen.append(("c", sees_loss)) or "")
    monkeypatch.setattr(_iss, "viewer_sees_loss", lambda u: False)
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: 1)
    monkeypatch.setattr(strategy_routes, "_sees_food", lambda u: True)
    strategy_routes._do_decisions({"id": 2, "role": "manager"})
    manager_view = type("V", (), {"_ask_sees_loss": False, "id": 1})()
    ask_cavnar._decisions_context(1, viewer=manager_view)
    tool = next(t for t in ask_cavnar_tools.TOOLS if t["spec"]["name"] == "read_decisions")
    assert tool.get("wants_viewer") is True
    tool["fn"](1, _viewer=manager_view)
    assert seen == [("h", False), ("c", False), ("h", False)]
