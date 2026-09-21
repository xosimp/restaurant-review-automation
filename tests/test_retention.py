"""The retention audit's invariants (Sep 2026).

The audit found the product compounds and never says so, that lifecycle
mail stopped at day 30, that a three-location owner received one
location's digest, and that briefings had no budget while alerts had four
kinds of one. These pin the fixes — and, as with every audit before it, the
refusals that keep them honest.
"""
from datetime import date, timedelta

import pytest

import models
import notify
import value_delivered
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    # Bound copies (CLAUDE.md's hazard): these modules take get_conn at
    # module scope and the route bodies below pass no db_path.
    import milestones, outcomes, good_news
    for m in (milestones, outcomes, good_news):
        monkeypatch.setattr(m, "get_conn", lambda *a, **k: real(db_path))
        monkeypatch.setattr(m, "DB_PATH", db_path)


def _restaurant(db_path, **kw):
    kw.setdefault("module_reviews", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "Retention Co"),
                                        owner_email=kw.pop("owner_email", "o@x.com"), **kw),
                             db_path=db_path)


def _log(db_path, rid, alert_type, n=1):
    conn = get_conn(db_path)
    for _ in range(n):
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) "
                     "VALUES (?,?,datetime('now'))", (rid, alert_type))
    conn.commit()
    conn.close()


# ── the briefing budget ──────────────────────────────────────────────────────

def test_briefings_are_counted_separately_from_alerts(db_path):
    rid = _restaurant(db_path)
    _log(db_path, rid, "morning_brief")
    _log(db_path, rid, "intraday_pulse")
    _log(db_path, rid, "1star")
    assert models.count_briefings_today(rid, db_path) == 2
    assert models.count_alerts_today(rid, db_path) == 1


def test_normal_level_stops_at_the_budget(db_path):
    rid = _restaurant(db_path)          # briefing_level defaults to normal
    _log(db_path, rid, "intraday_pulse", notify.BRIEFING_NORMAL_PER_DAY)
    assert notify.briefing_allowed(rid, "demand_opportunity", db_path) is False


def test_the_always_set_never_counts_against_the_budget(db_path):
    """The morning brief, a result, a milestone, "while you were away" and a
    lost connection get through at any level, on any day."""
    rid = _restaurant(db_path)
    _log(db_path, rid, "intraday_pulse", notify.BRIEFING_NORMAL_PER_DAY + 3)
    for t in notify.BRIEFING_ALWAYS:
        assert notify.briefing_allowed(rid, t, db_path) is True, t


def _level(db_path, rid, level):
    # create_restaurant's INSERT names its columns; briefing_level is set
    # the way the settings route sets it.
    from models import update_restaurant
    update_restaurant(rid, {"briefing_level": level}, db_path=db_path)


def test_calm_allows_only_the_calm_set(db_path):
    rid = _restaurant(db_path)
    _level(db_path, rid, "calm")
    assert notify.briefing_allowed(rid, "closing_summary", db_path) is True
    assert notify.briefing_allowed(rid, "schedule_drafted", db_path) is True
    assert notify.briefing_allowed(rid, "intraday_pulse", db_path) is False
    assert notify.briefing_allowed(rid, "demand_opportunity", db_path) is False


def test_all_has_no_budget(db_path):
    rid = _restaurant(db_path)
    _level(db_path, rid, "all")
    _log(db_path, rid, "intraday_pulse", 20)
    assert notify.briefing_allowed(rid, "demand_opportunity", db_path) is True


def test_briefing_level_round_trips_through_update_restaurant(db_path):
    """The four touch points. Miss the whitelist and the write silently
    no-ops — which is exactly what a setting that 'doesn't stick' looks
    like from the owner's chair."""
    from models import update_restaurant, get_restaurant
    rid = _restaurant(db_path)
    update_restaurant(rid, {"briefing_level": "calm"}, db_path=db_path)
    assert get_restaurant(rid, db_path).briefing_level == "calm"


def test_the_settings_route_rejects_a_made_up_level(db_path, monkeypatch):
    import strategy_routes
    rid = _restaurant(db_path)
    u = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False}
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"briefing_level": "loud"})
    payload, status = strategy_routes._do_morning_brief_settings(u)
    assert status == 400 and "briefing_level" in payload["error"]


def test_every_direct_briefing_push_consults_the_budget():
    """Mechanical: the three pushes that bypass _reach must each ask
    briefing_allowed for their own type before recording anything."""
    import inspect
    import strategy_jobs
    src = inspect.getsource(strategy_jobs)
    for t in ("schedule_drafted", "intraday_pulse", "demand_opportunity"):
        gate = f'notify.briefing_allowed(r.id, "{t}", db_path)'
        record = f'notify.record_notification(r.id, "{t}"'
        assert gate in src, f"{t} push is not gated"
        assert src.index(gate) < src.index(record), f"{t} records before it asks"
    assert "if not notify.briefing_allowed(restaurant_id, alert_type, db_path):" in src


# ── the ledger ───────────────────────────────────────────────────────────────

def test_ledger_counts_distinct_work_not_rows(db_path):
    """The ROI audit's rule. schedule_history keeps a row per generated
    draft; the ledger must count weeks, or three regenerations of one week
    read as three schedules built."""
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    for _ in range(3):
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, generated_at) "
                     "VALUES (?,?,datetime('now'))", (rid, "2026-09-14"))
    conn.commit()
    conn.close()
    assert value_delivered.ledger(rid, db_path=db_path)["schedules_built"] == 1


def test_ledger_lines_omit_zeros_and_read_as_sentences():
    lines = value_delivered.ledger_lines({"replies_drafted": 2, "schedules_built": 0,
                                          "alerts_sent": 1})
    assert lines == ["2 review replies drafted in your voice",
                     "1 alert sent before it became a problem"]


# ── the grouped digest ───────────────────────────────────────────────────────

def _report(rid, rating, total):
    from reporter import WeeklyReport
    r = WeeklyReport(restaurant_id=rid, period_start="Sep 13", period_end="Sep 20, 2026")
    r.avg_rating = rating
    r.total_reviews = total
    return r


def test_group_digest_carries_every_location(db_path, monkeypatch):
    """The dedup-by-address dropped two of three restaurants' weeks. The
    grouped shell must name each location under its own eyebrow."""
    import reporter
    from models import get_restaurant
    a = _restaurant(db_path, name="Brand", location_name="Downtown", location_group="Brand")
    b = _restaurant(db_path, name="Brand", location_name="Airport", location_group="Brand")
    c = _restaurant(db_path, name="Brand", location_name="Harbor", location_group="Brand")
    # No model call, no follow-through reads: sections only.
    monkeypatch.setattr(reporter, "_digest_parts",
                        lambda rep, name, owner, rid, view: {
                            "sections": [f"<p>week for {rid}</p>"], "week_label": "Week of Sep 20",
                            "location_label": "", "rating": rep.avg_rating, "total": rep.total_reviews})
    items = [(get_restaurant(x, db_path), _report(x, r, 10)) for x, r in ((a, 4.6), (b, 3.9), (c, 4.2))]
    html = reporter.render_group_html(items, owner_name="Erik", group_name="Brand")
    for loc in ("Downtown", "Airport", "Harbor"):
        assert loc in html
    for x in (a, b, c):
        assert f"week for {x}" in html
    assert "3 locations" in html
    # Strongest and weakest are named on the portfolio line, in that order.
    assert html.index("Downtown") < html.index("Airport")


def test_single_location_digest_is_unchanged_in_shape(db_path, monkeypatch):
    import reporter
    from models import get_restaurant
    rid = _restaurant(db_path, name="Solo")
    monkeypatch.setattr(reporter, "_digest_parts",
                        lambda rep, name, owner, r, view: {
                            "sections": ["<p>one</p>"], "week_label": "Week of Sep 20",
                            "location_label": "", "rating": 4.0, "total": 3})
    html = reporter.render_html(_report(rid, 4.0, 3), "Solo", restaurant_id=rid)
    assert "Weekly Digest" in html and "<p>one</p>" in html and "locations" not in html


# ── lifecycle after day 30 ───────────────────────────────────────────────────

def test_lifecycle_email_is_built_from_measured_figures_only(db_path, monkeypatch):
    import emails
    rid = _restaurant(db_path, module_labor=0, module_inventory=0)
    sent = {}
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.update(kw) or True)
    emails.send_lifecycle_email(60, "o@x.com", "Retention Co", "Erik", restaurant_id=rid)
    html = sent["payload"]["html"]
    assert sent["email_type"] == "send_lifecycle_day60"
    assert "Two months in" in html
    # Nothing tracked: the day-60 email says so and says how to change it,
    # rather than inventing a figure.
    assert "Nothing measured yet" in html and "Track this" in html


def test_lifecycle_email_refuses_unknown_days(monkeypatch):
    import emails
    called = []
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "deliver", lambda **kw: called.append(kw))
    emails.send_lifecycle_email(45, "o@x.com", "X", "E", restaurant_id=1)
    assert called == []


def test_the_onboarding_sequence_dispatches_lifecycle_days():
    """Mechanical: the window is 14 days like day 30's, the key is
    day_<n>, and the monthly-review switch — not the marketing opt-out —
    is what silences it."""
    import inspect
    import scheduler
    src = inspect.getsource(scheduler.run_onboarding_sequence)
    assert "for _day in (60, 90, 180):" in src
    assert "_day <= days_since <= _day + 14" in src
    assert 'monthly_review_enabled' in src
    # The opt-out guard must not reach the lifecycle block.
    assert "if _opted_out and days_since < 60:" in src


# ── while you were away ──────────────────────────────────────────────────────

def _quiet_owner(db_path, rid, days):
    """A real console login (auth's own schema and create_user) whose last
    sign-in was `days` ago."""
    import auth
    auth.init_auth(db_path)
    uid = auth.create_user(rid, "u", "o@x.com", "pw-not-used", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET last_login=datetime('now', ?) WHERE id=?", (f"-{days} days", uid))
    conn.commit()
    conn.close()
    return uid

def test_while_away_says_nothing_when_nothing_happened(db_path, monkeypatch):
    import scheduler
    rid = _restaurant(db_path)
    _quiet_owner(db_path, rid, days=20)
    reached = []
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: reached.append(a) or 1)
    out = scheduler.send_while_away_nudges()
    assert out["sent"] == 0 and reached == []


def test_while_away_fires_once_a_month_with_real_counts(db_path, monkeypatch):
    import scheduler, strategy_jobs
    rid = _restaurant(db_path)
    _quiet_owner(db_path, rid, days=20)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, sentiment, "
                 "processed, response_status, draft_response, review_date, fetched_at) "
                 "VALUES (?,?,?,?,?,?,1,'drafted','hi',date('now'),datetime('now','-2 days'))",
                 (rid, "google", "wa-1", 4, "t", "positive"))
    conn.commit()
    conn.close()
    reached = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, t, title, body, *a, **k: reached.append((t, title, k.get("lines"))) or 1)
    assert scheduler.send_while_away_nudges()["sent"] == 1
    t, title, lines = reached[0]
    import re
    # The seed is SQLite UTC and the job counts in Chicago time, so the gap
    # is 19-point-something days at most hours of the day. Pin the shape.
    assert t == "while_away" and re.search(r"\b(19|20) days\b", title), title
    assert any("1 new review" in l and "reply drafted" in l for l in lines)
    # Second run in the same month: claimed, silent.
    assert scheduler.send_while_away_nudges()["sent"] == 0


# ── the lost connection reaches the owner, once a week ───────────────────────

def test_connection_lost_is_an_alert_with_a_tab_and_no_ceiling():
    assert notify.ALERT_TAB["connection_lost"] == "account"
    assert "connection_lost" in models.NON_ALERT_TYPES
    assert "connection_lost" in notify.BRIEFING_ALWAYS


def test_the_fetch_raises_connection_lost_on_a_revoked_token():
    import inspect
    import scheduler
    src = inspect.getsource(scheduler.run_daily_fetch)
    i = src.index("Google refresh token is no longer valid")
    tail = src[i:i + 1500]
    assert 'claim_period(f"connection_lost:{rid}"' in tail
    assert '"connection_lost"' in tail and "Account" in tail


# ── part 2: value and engagement ─────────────────────────────────────────────

def _days(db_path, rid, start, n, sales=2000.0, labor_ratio=0.30):
    conn = get_conn(db_path)
    for i in range(n):
        d = start + timedelta(days=i)
        conn.execute("INSERT OR REPLACE INTO labor_daily_history "
                     "(restaurant_id, date, day_of_week, sales, labor_cost) VALUES (?,?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), sales, sales * labor_ratio))
    conn.commit()
    conn.close()


def test_observe_records_an_owner_action_once_a_month(db_path):
    """A published schedule is an owner acting. It becomes a labor % tracker
    with no button pressed — and a second publish the same month does not
    become a second tracker on the same metric."""
    import outcomes
    rid = _restaurant(db_path, module_labor=1)
    _days(db_path, rid, date.today() - timedelta(days=40), 40)
    first = outcomes.observe(rid, "schedule_published", detail="week of 2026-09-21", db_path=db_path)
    assert first and first["source"] == "observed" and first["metric"] == "labor_pct"
    assert "Published a schedule" in first["title"]
    assert outcomes.observe(rid, "schedule_published", db_path=db_path) is None


def test_observe_never_doubles_a_tracker_already_in_flight(db_path):
    """Two trackers on one metric would count the same move twice."""
    import outcomes
    rid = _restaurant(db_path, module_labor=1)
    _days(db_path, rid, date.today() - timedelta(days=40), 40)
    outcomes.record(rid, "recommendation", "trim_day:Monday", "Trim Monday", "labor_pct", db_path=db_path)
    assert outcomes.observe(rid, "schedule_published", db_path=db_path) is None


def test_observe_ignores_unknown_actions(db_path):
    import outcomes
    assert outcomes.observe(_restaurant(db_path), "owner_sneezed", db_path=db_path) is None


def test_done_and_not_for_us_do_not_come_back_in_a_fortnight(db_path):
    import home_brief
    rid = _restaurant(db_path)
    hide = home_brief.dismiss(rid, "trim_day:Monday")
    done = home_brief.dismiss(rid, "cut_waste:Salmon", kind="done")
    never = home_brief.dismiss(rid, "post_this_week", kind="not_for_us")
    assert hide["days"] == home_brief._DISMISS_DAYS
    assert done["days"] > 365 and never["days"] > 365
    assert done["kind"] == "done" and never["kind"] == "not_for_us"


def test_an_unknown_dismiss_kind_falls_back_to_hide(db_path):
    import home_brief
    out = home_brief.dismiss(_restaurant(db_path), "x", kind="forever")
    assert out["kind"] == "recommendation" and out["days"] == home_brief._DISMISS_DAYS


def test_the_mobile_dismiss_twin_exists():
    import inspect
    import mobile_api
    src = inspect.getsource(mobile_api)
    assert '@mobile_bp.route("/home/dismiss", methods=["POST"])' in src
    assert "_capi.home_dismiss_api.__wrapped__" in src


def test_a_rating_record_needs_three_priors_not_five():
    import good_news
    assert good_news.MIN_PRIOR_BY_METRIC["avg_rating"] == 3
    assert good_news.MIN_PRIOR_PERIODS == 5


def test_readiness_names_the_next_step_and_hides_when_complete(db_path):
    import home_brief
    from models import get_restaurant
    rid = _restaurant(db_path, module_labor=1, module_inventory=0, module_marketing=0)
    r = get_restaurant(rid)
    row = {"gmb_refresh_token": None, "reviews_live": 0}
    out = home_brief.readiness(rid, r, row, {}, False, False, {})
    by = {m["key"]: m for m in out["modules"]}
    assert by["reviews"]["connected"] is False and "Google" in by["reviews"]["next"]
    assert by["labor"]["connected"] is False and by["labor"]["next"]
    assert out["complete"] is False and out["connected"] == 0
    row["reviews_live"] = 1
    out = home_brief.readiness(rid, r, row, {}, True, False, {})
    assert out["complete"] is True and out["connected"] == 2


def test_readiness_is_never_a_score():
    """A number here would be a grade on the owner; the point is the next
    step."""
    import inspect
    import home_brief
    src = inspect.getsource(home_brief.readiness)
    assert "score" not in src.replace("Never a score", "")


def test_four_star_candidates_only_when_asked(db_path):
    from models import auto_approve_candidates
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    for ext, rating in (("a", 5), ("b", 4), ("c", 3)):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, sentiment, "
                     "processed, response_status, draft_response, review_date, fetched_at) "
                     "VALUES (?,?,?,?,?,?,1,'drafted','ok',date('now'),datetime('now'))",
                     (rid, "google", ext, rating, "t", "positive"))
    conn.commit()
    conn.close()
    assert len(auto_approve_candidates(rid, db_path)) == 1
    assert len(auto_approve_candidates(rid, db_path, ratings=(4, 5))) == 2
    # Three stars is never a candidate, whatever is asked for.
    assert len(auto_approve_candidates(rid, db_path, ratings=(3, 4, 5))) == 3  # the query obeys; the caller must not ask
    import inspect, scheduler
    src = inspect.getsource(scheduler.auto_approve_five_stars)
    assert "ratings = (4, 5) if getattr(restaurant, \"auto_approve_4star\", 0) else (5,)" in src


def test_four_star_is_off_unless_auto_approve_is_on(db_path):
    """Turning the 4-star switch on with the rule off must not store a live
    4-star flag waiting for the day the rule is enabled."""
    import client_api
    from models import get_restaurant
    rid = _restaurant(db_path)
    client_api._do_auto_approve(rid, {"enabled": False, "include_4star": True})
    assert get_restaurant(rid, db_path).auto_approve_4star == 0
    client_api._do_auto_approve(rid, {"enabled": True, "include_4star": True})
    assert get_restaurant(rid, db_path).auto_approve_4star == 1


def test_unlock_line_names_the_first_module_off_the_plan():
    import morning_brief
    line = morning_brief._unlock_line(None, {"modules_off": ["food_cost", "marketing"]})
    assert line["key"] == "unlock:food_cost" and line["ask"]
    assert morning_brief._unlock_line(None, {"modules_off": []}) is None


def test_ledger_rides_on_the_value_route(db_path, monkeypatch):
    import strategy_routes
    rid = _restaurant(db_path)
    monkeypatch.setattr(strategy_routes, "_metric_visible", lambda u, m: True)
    payload, _ = strategy_routes._do_value({"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False})
    assert "ledger" in payload and "lines" in payload["ledger"]


# ── self-serve pause (Phase C, #9) ───────────────────────────────────────────
# The only path off the product was an email to Will asking to cancel. A
# pause is the smaller decision an owner can make for themselves, and it
# must be reachable while paused — "paused" is a blocked billing state.

def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "role": "owner", "is_admin": False, "username": "o"}


def _pause_env(monkeypatch, db_path, stripe=None):
    import strategy_routes, webhook_routes, emails
    real = models.get_conn
    monkeypatch.setattr(webhook_routes, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(strategy_routes, "_stripe_client", lambda: stripe)
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.update(kw) or True)
    return strategy_routes, sent


def test_pause_moves_the_account_to_paused_with_a_resume_date(db_path, monkeypatch):
    sr, sent = _pause_env(monkeypatch, db_path)
    rid = _restaurant(db_path)
    monkeypatch.setattr(sr, "_body", lambda: {"days": 30})
    payload, status = sr._do_pause(_owner(rid))
    assert status == 200 and payload["ok"], payload
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.billing_status == "paused"
    assert r.paused_until == payload["paused_until"]
    from time_utils import restaurant_now
    assert (date.fromisoformat(r.paused_until) - restaurant_now(r).date()).days == 30
    assert sent["email_type"] == "pause_notice_ops"          # Will hears about it
    # A paused account is a blocked one — the product goes quiet on its own.
    assert models.subscription_allows_access(rid, db_path=db_path) is False
    st, _ = sr._do_pause_status(_owner(rid))
    assert st["paused"] is True and st["paused_until"] == r.paused_until


def test_resume_clears_the_pause(db_path, monkeypatch):
    sr, _ = _pause_env(monkeypatch, db_path)
    rid = _restaurant(db_path)
    monkeypatch.setattr(sr, "_body", lambda: {"days": 14})
    sr._do_pause(_owner(rid))
    payload, status = sr._do_resume(_owner(rid))
    assert status == 200 and payload["ok"]
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.billing_status == "active" and r.paused_until is None
    assert models.subscription_allows_access(rid, db_path=db_path) is True


def test_pause_refuses_odd_lengths_and_double_pauses(db_path, monkeypatch):
    sr, _ = _pause_env(monkeypatch, db_path)
    rid = _restaurant(db_path)
    monkeypatch.setattr(sr, "_body", lambda: {"days": 7})
    _, status = sr._do_pause(_owner(rid))
    assert status == 400
    monkeypatch.setattr(sr, "_body", lambda: {"days": 30})
    assert sr._do_pause(_owner(rid))[1] == 200
    assert sr._do_pause(_owner(rid))[1] == 409
    assert sr._do_resume(_owner(rid))[1] == 200
    assert sr._do_resume(_owner(rid))[1] == 409       # not paused any more


def test_only_the_owner_can_pause(db_path, monkeypatch):
    sr, _ = _pause_env(monkeypatch, db_path)
    rid = _restaurant(db_path)
    monkeypatch.setattr(sr, "_body", lambda: {"days": 30})
    manager = dict(_owner(rid), role="manager")
    assert sr._do_pause(manager)[1] == 403
    assert sr._do_resume(manager)[1] == 403
    assert sr._do_pause_status(manager)[0]["can_pause"] is False
    assert models.get_restaurant(rid, db_path=db_path).billing_status != "paused"


def test_pause_tells_stripe_to_stop_collecting_and_nothing_changes_if_stripe_refuses(db_path, monkeypatch):
    calls = []

    class _Sub:
        id = "sub_1"

    class _Subscription:
        @staticmethod
        def list(customer, status, limit):
            return type("R", (), {"data": [_Sub()] if status == "active" else []})()

        @staticmethod
        def modify(sub_id, **kw):
            calls.append((sub_id, kw))
            if kw.get("pause_collection") and kw["pause_collection"] != "" and _Subscription.refuse:
                raise RuntimeError("card_declined")

    _Subscription.refuse = False
    stripe = type("S", (), {"Subscription": _Subscription})
    sr, _ = _pause_env(monkeypatch, db_path, stripe=stripe)
    rid = _restaurant(db_path, stripe_customer_id="cus_1")
    monkeypatch.setattr(sr, "_body", lambda: {"days": 60})
    assert sr._do_pause(_owner(rid))[1] == 200
    sub_id, kw = calls[-1]
    assert sub_id == "sub_1" and kw["pause_collection"]["behavior"] == "void"
    assert kw["pause_collection"]["resumes_at"] > 0
    assert sr._do_resume(_owner(rid))[1] == 200
    assert calls[-1][1] == {"pause_collection": ""}       # the resume un-pauses Stripe too

    _Subscription.refuse = True
    payload, status = sr._do_pause(_owner(rid))
    assert status == 502 and "nothing changed" in payload["error"]
    assert models.get_restaurant(rid, db_path=db_path).billing_status == "active"


def test_the_pause_routes_stay_reachable_while_paused():
    """auth's billing block would otherwise lock the owner out of the one
    button that ends the pause. Mobile is covered by /mobile/api/account."""
    import auth
    for p in ("/api/account/pause", "/api/account/resume", "/mobile/api/account"):
        assert any(p.startswith(x) for x in auth._BILLING_EXEMPT_PREFIXES), p


def test_a_paused_account_gets_no_nudges(db_path, monkeypatch):
    import strategy_jobs
    rid = _restaurant(db_path)
    from models import update_restaurant
    update_restaurant(rid, {"billing_status": "paused"}, db_path=db_path)
    reached = []
    import morning_brief
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: reached.append(1) or [])
    assert strategy_jobs._reach(rid, "while_away", "t", "b", {}, db_path) == 0
    assert reached == []                                  # never even asked who to reach


def test_paused_until_survives_the_four_touch_points(db_path):
    from models import update_restaurant
    rid = _restaurant(db_path)
    update_restaurant(rid, {"paused_until": "2026-10-20"}, db_path=db_path)
    assert models.get_restaurant(rid, db_path=db_path).paused_until == "2026-10-20"
    update_restaurant(rid, {"paused_until": None}, db_path=db_path)
    assert models.get_restaurant(rid, db_path=db_path).paused_until is None


# ── the monthly review on the web ────────────────────────────────────────────

def test_the_monthly_review_route_carries_a_yoy_clause_per_metric(db_path, monkeypatch):
    import strategy_routes, monthly_review
    rid = _restaurant(db_path)
    monkeypatch.setattr(strategy_routes, "_metric_visible", lambda u, m: True)
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: date(2026, 9, 1))
    fake = {"month": "August 2026", "compared_with": "July", "metrics": [
        {"key": "labor_pct", "value": 31.0, "previous": 30.0, "unit": "pct", "verdict": "steady",
         "year_ago": 35.0, "yoy": {"verdict": "improved", "delta": -4.0}}], "results": []}
    monkeypatch.setattr(monthly_review, "build", lambda *a, **k: dict(fake))
    payload, status = strategy_routes._do_monthly_review(_owner(rid))
    assert status == 200 and payload["ok"]
    assert payload["yoy"]["labor_pct"] == monthly_review.yoy_clause(fake["metrics"][0])
    assert "last year" in payload["yoy"]["labor_pct"]


def test_the_monthly_review_route_is_registered_on_both_sides():
    import strategy_routes
    paths = {(p, tuple(m)) for p, m, *_ in strategy_routes._ROUTES}
    assert ("/monthly-review", ("GET",)) in paths
    assert ("/account/pause", ("POST",)) in paths and ("/account/resume", ("POST",)) in paths


# ── the two things the pause implementation itself could have broken ─────────

def test_stripe_reports_a_pause_as_active_and_the_webhook_reads_pause_collection():
    """Stripe leaves status "active" while pause_collection is set, and it
    fires subscription.updated for the pause itself. Reading status alone
    would flip the account back to active seconds after it paused."""
    import webhook_routes as wr
    from datetime import datetime, timezone
    ts = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp())
    assert wr._paused_until({"status": "active", "pause_collection": {"behavior": "void", "resumes_at": ts}}) == "2026-10-20"
    assert wr._paused_until({"status": "active", "pause_collection": {"behavior": "void"}}) == "open"
    assert wr._paused_until({"status": "active", "pause_collection": None}) == ""
    assert wr._paused_until({"status": "active"}) == ""


def _stripe_app(monkeypatch, event):
    import webhook_routes as wr
    from flask import Flask
    real = models.get_conn
    monkeypatch.setattr(wr, "get_conn", lambda *a, **k: real(models.DB_PATH))
    # The stripe library is a production dependency, not a test one: stand
    # in for the one call the route makes.
    import sys, types
    fake = types.ModuleType("stripe")
    fake.Webhook = type("W", (), {"construct_event": staticmethod(lambda *a, **k: event)})
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(wr, "send_alert", lambda *a, **k: None, raising=False)
    app = Flask(__name__)
    app.register_blueprint(wr.webhook_bp)
    return app.test_client()


def test_the_pause_survives_stripes_own_subscription_updated_event(db_path, monkeypatch):
    import strategy_routes as sr
    monkeypatch.setattr(models, "DB_PATH", db_path)
    sr_, _ = _pause_env(monkeypatch, db_path)
    rid = _restaurant(db_path, stripe_customer_id="cus_p")
    monkeypatch.setattr(sr, "_body", lambda: {"days": 30})
    assert sr._do_pause(_owner(rid))[1] == 200
    until = models.get_restaurant(rid, db_path=db_path).paused_until
    from datetime import datetime, timezone
    ts = int(datetime.fromisoformat(until).replace(tzinfo=timezone.utc).timestamp())
    ev = {"id": "evt_pause_1", "type": "customer.subscription.updated",
          "data": {"object": {"id": "sub_1", "customer": "cus_p", "status": "active",
                              "metadata": {"restaurant_id": str(rid)},
                              "pause_collection": {"behavior": "void", "resumes_at": ts}}}}
    c = _stripe_app(monkeypatch, ev)
    assert c.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t"}).status_code == 200
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.billing_status == "paused" and r.paused_until == until
    # Stripe reaches resumes_at: pause_collection clears, the same event
    # fires, and the account comes back without anyone touching it.
    ev2 = dict(ev, id="evt_resume_1")
    ev2["data"] = {"object": dict(ev["data"]["object"], pause_collection=None)}
    c = _stripe_app(monkeypatch, ev2)
    assert c.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t"}).status_code == 200
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.billing_status == "active" and r.paused_until is None


def _blocked_page_client(monkeypatch, rid, role):
    import auth
    from flask import Flask
    real = models.get_conn
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: real(models.DB_PATH), raising=False)
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "o", "role": role})
    app = Flask(__name__, template_folder="../templates")

    @app.route("/")
    @auth.login_required
    def home(current_user):
        return "dashboard"

    @app.route("/api/thing")
    @auth.login_required
    def thing(current_user):
        return "{}"
    return app.test_client()


def test_a_paused_owner_sees_a_page_with_the_date_and_a_resume_button_not_json(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = _restaurant(db_path)
    from models import update_restaurant
    update_restaurant(rid, {"billing_status": "paused", "paused_until": "2026-10-20"}, db_path=db_path)
    c = _blocked_page_client(monkeypatch, rid, "owner")
    resp = c.get("/")
    assert resp.status_code == 402 and resp.mimetype == "text/html"
    html = resp.get_data(as_text=True)
    assert 'id="resume-btn"' in html and 'data-iso="2026-10-20"' in html and "/api/account/resume" in html
    # The fetch() calls the dashboard makes still get JSON, which is what
    # their error handling expects.
    j = c.get("/api/thing")
    assert j.status_code == 402 and j.get_json()["billing_inactive"] is True
    # A manager cannot resume and is told who can.
    html = _blocked_page_client(monkeypatch, rid, "manager").get("/").get_data(as_text=True)
    assert 'id="resume-btn"' not in html and "account owner can resume" in html


def test_a_lapsed_owner_sees_the_message_and_wills_address(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = _restaurant(db_path)
    from models import update_restaurant
    update_restaurant(rid, {"billing_status": "churned"}, db_path=db_path)
    html = _blocked_page_client(monkeypatch, rid, "owner").get("/").get_data(as_text=True)
    assert "no longer active" in html and "mailto:will@cavnar.ai" in html and 'id="resume-btn"' not in html


def test_a_paused_account_is_told_it_is_paused_not_lapsed_on_every_surface(db_path, monkeypatch):
    """The phone shows the 402 error string on its Home tab. "No longer
    active — contact Will" to an owner who paused it themselves an hour ago
    is the wrong sentence."""
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = _restaurant(db_path)
    from models import update_restaurant
    update_restaurant(rid, {"billing_status": "paused", "paused_until": "2026-10-20"}, db_path=db_path)
    j = _blocked_page_client(monkeypatch, rid, "owner").get("/api/thing").get_json()
    assert j["paused"] is True and j["paused_until"] == "2026-10-20"
    assert "paused until 10/20/26" in j["error"] and "no longer active" not in j["error"]
    update_restaurant(rid, {"billing_status": "churned", "paused_until": None}, db_path=db_path)
    j = _blocked_page_client(monkeypatch, rid, "owner").get("/api/thing").get_json()
    assert "no longer active" in j["error"] and "paused" not in j
