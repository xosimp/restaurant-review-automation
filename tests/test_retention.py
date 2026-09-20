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
