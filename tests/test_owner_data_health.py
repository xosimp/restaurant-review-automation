"""Data Freshness audit (9/24/26), workstream O — the owner surfaces' server
half: when each source next refreshes and what a pending one says (#27),
"current" only inside a source's cadence (#27), Account → Connections lines
from the registry in the restaurant's clock (#17, #34), the Marketing metrics
line (#36), Home's data_health block and stale-POS card (#17, #21), the
phone's freshness_unavailable flag (#37), the morning brief's data-health
line, all-clear gate, held lines and dates (#12), the data_source_down alert
and its back-to-normal notice (#18), the rating alert's age gate and the
monthly email's honesty (#38), the activity feed's clock and cache (#46), and
the confidence impact on every K1 object (#22)."""
import inspect
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import activity
import client_api
import confidence_engine as ce
import data_freshness as df
import data_health as dh
import home_brief
import mobile_api
import models
import morning_brief
import notify
from models import Restaurant, create_restaurant, get_conn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LA = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import auth
    for mod in (models, auth, client_api, mobile_api, home_brief, morning_brief, activity):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    import ai_utils
    c = real(db_path)
    c.executescript(ai_utils._USAGE_TABLE_SQL)
    c.commit()
    c.close()
    dh.invalidate()
    home_brief.invalidate()
    activity._CACHE.clear()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model called")))
    yield
    dh.invalidate()


def _rid(db_path, **kw):
    kw.setdefault("name", "Owner Cafe")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _set(db_path, rid, **cols):
    conn = get_conn(db_path)
    for k, v in cols.items():
        conn.execute(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    conn.commit()
    conn.close()


def _day(db_path, rid, d, sales=4200.0, labor=1100.0):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_cost, sales, total_hours) "
                 "VALUES (?,?,?,?,?)", (rid, d.isoformat(), labor, sales, 80))
    conn.commit()
    conn.close()


def _local_today(tz, now=None):
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()


def _chi(dt):
    return dt.astimezone(ZoneInfo("America/Chicago")).strftime("%Y-%m-%dT%H:%M:%S")


def _user(rid, uid=1):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner",
            "role": "client", "is_admin": 0, "email": "o@x.test"}


# ── when a source next refreshes (#27, DH4-18) ─────────────────────────────

def test_slots_are_the_schedulers_own_times():
    """SLOTS reads the scheduler's clock; if a job moves, this fails."""
    src = open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8").read()
    assert dh.SLOTS["pos"][0] == (3,) and '_due(now, 3) and _ops.claim_period("pos_sync"' in src
    assert dh.SLOTS["marketing"][0] == (4,) and '_due(now, 4) and _ops.claim_period("marketing_metrics_sync"' in src
    assert dh.SLOTS["reviews"][0] == (8, 12, 16, 20) and "_latest_slot(now, (8, 12, 16, 20))" in src
    assert dh.SLOTS["competitor"] == ((6,), 0) and \
        '_due(now, 6) and now.weekday() == 0 and _ops.claim_period("competitor_analysis"' in src
    assert dh.SLOTS["visibility"] == ((7,), 0) and \
        '_due(now, 7) and now.weekday() == 0 and _ops.claim_period("ai_visibility"' in src


def test_next_slot_is_said_in_the_restaurants_clock():
    now = datetime(2026, 9, 24, 22, 0, tzinfo=timezone.utc)          # 5pm Chicago, 6pm New York
    pos = dh.next_slot("pos", now)
    assert pos == datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)    # 3am CDT
    assert dh.expected_phrase(pos, "America/New_York", now) == "tonight 4am"
    assert dh.expected_phrase(dh.next_slot("reviews", now), "America/New_York", now) == "tonight 9pm"
    assert dh.expected_phrase(dh.next_slot("competitor", now), "America/New_York", now) == "9/28/26 · 7am"
    assert dh.next_slot("inventory", now) is None
    assert dh.clock(datetime(2026, 9, 24, 3, 2)) == "3:02am" and dh.clock(datetime(2026, 9, 24, 16, 0)) == "4pm"


def test_a_pos_waiting_on_its_first_sync_says_when_and_is_never_scored(db_path):
    rid = _rid(db_path, timezone="America/Chicago")
    _set(db_path, rid, toast_restaurant_guid="guid-1")
    now = datetime(2026, 9, 24, 22, 0, tzinfo=timezone.utc)
    snap = dh.snapshot(rid, db_path=db_path, now=now, use_cache=False)
    pos = next(s for s in snap["sources"] if s["key"] == "pos")
    assert pos["pending"] and pos["line"] == "POS sales: first sync runs tonight 3am"
    assert pos["expected_line"] == "POS sync runs tonight 3am" and pos["expected_by"] == "2026-09-25T08:00:00Z"
    assert snap["overall"]["pct"] is None and snap["overall"]["state"] == "pending"
    assert snap["connections"]["pos"]["line"] == "Connected — first sync runs tonight 3am"


def test_current_is_said_only_inside_a_sources_cadence():
    now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)
    fresh = {"key": "pos", "state": "current", "last_ok_at": (now - timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")}
    old = dict(fresh, last_ok_at=(now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"))
    assert dh.counts_as_current(fresh, now) and not dh.counts_as_current(old, now)
    assert dh.counts_as_current({"key": "inventory", "state": "current"}, now)
    assert not dh.counts_as_current(dict(fresh, error="sync failing"), now)
    assert not dh.counts_as_current(dict(fresh, state="aging"), now)
    # Home's entries carry the source key under "source".
    assert not dh.counts_as_current({"source": "pos", "key": "labor", "state": "current",
                                     "last_ok_at": old["last_ok_at"]}, now)


# ── Account → Connections (#17, #34) ────────────────────────────────────────

def test_pos_connection_line_is_the_registry_in_local_time(db_path):
    rid = _rid(db_path, timezone=LA)
    _set(db_path, rid, toast_restaurant_guid="guid-1")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    synced = now - timedelta(hours=1)
    _set(db_path, rid, toast_last_synced=synced.isoformat())
    _day(db_path, rid, _local_today(LA, now) - timedelta(days=1))
    dh.record_attempt(rid, "pos", True, provider="toast", db_path=db_path, now=synced)
    r = models.get_restaurant(rid, db_path=db_path)
    line = dh.connection_lines(r, now=now, db_path=db_path)["pos"]
    want = dh.when_local(synced, LA, now)
    assert line["line"] == f"Last sync {want} · Sales through {ce._mdy((_local_today(LA, now) - timedelta(days=1)).isoformat())}"
    assert line["tone"] == "ok"
    # Local, never UTC: Los Angeles is never on UTC's clock.
    assert dh.clock(synced.astimezone(ZoneInfo(LA))) in want and "UTC" not in line["line"]


def test_pos_connection_line_goes_bad_when_sales_stopped(db_path):
    rid = _rid(db_path, timezone="America/Chicago")
    _set(db_path, rid, toast_restaurant_guid="guid-1",
         toast_last_synced=datetime.now(timezone.utc).isoformat())
    _day(db_path, rid, _local_today("America/Chicago") - timedelta(days=10))
    r = models.get_restaurant(rid, db_path=db_path)
    line = dh.connection_lines(r, db_path=db_path)["pos"]
    assert line["tone"] == "bad" and "Sales through" in line["line"]


def test_a_disconnected_pos_reads_as_a_warning_with_nothing_promised(db_path, monkeypatch):
    """A POS removed after use keeps its last data's recency (workstream J's
    "disconnected" state): amber, "disconnected", no next sync, no Sync now."""
    st = {"key": "pos", "label": "POS", "state": "disconnected", "pct": 70, "as_of": "9/20/26",
          "as_of_iso": "2026-09-20", "basis": "Toast sales through 9/20/26 · disconnected", "provider": "toast"}
    line = dh.source_line(st, tz="America/Chicago", pos_connected=False)
    assert line["tone"] == "warn" and line["expected_line"] is None and not line["can_sync_now"]
    assert not dh.counts_as_current(st) and not dh.is_pending(st)
    monkeypatch.setattr(df, "source_state", lambda r, k, **kw: dict(st))
    rid = _rid(db_path)
    pos = dh.connection_lines(models.get_restaurant(rid, db_path=db_path), db_path=db_path)["pos"]
    assert pos["tone"] == "warn" and pos["line"] == "Disconnected · Sales through 9/20/26" and pos["next"] is None


def test_google_connection_line_says_checked_and_next_or_missed(db_path):
    rid = _rid(db_path, timezone="America/Chicago")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _set(db_path, rid, gmb_refresh_token="tok", last_fetched_at=_chi(now - timedelta(minutes=30)))
    g = dh.connection_lines(models.get_restaurant(rid, db_path=db_path), now=now, db_path=db_path)["google"]
    assert g["line"].startswith("Checked ") and "next check" in g["line"] and g["tone"] == "ok"
    _set(db_path, rid, last_fetched_at=_chi(now - timedelta(days=3)))
    g = dh.connection_lines(models.get_restaurant(rid, db_path=db_path), now=now, db_path=db_path)["google"]
    assert g["line"].startswith("Last check ") and "checks missed" in g["line"] and g["tone"] == "bad"


def test_mobile_account_and_reviews_carry_the_lines(db_path):
    rid = _rid(db_path, timezone="America/Chicago")
    now = datetime.now(timezone.utc)
    _set(db_path, rid, gmb_refresh_token="tok", last_fetched_at=_chi(now - timedelta(days=3)),
         toast_restaurant_guid="guid-1")
    payload, st = mobile_api._do_mobile_account(_user(rid))
    assert st == 200
    assert payload["connections"]["google_business"]["fetch_line"]["line"].startswith("Last check ")
    assert payload["connections"]["pos_line"]["line"].startswith("Connected — first sync")
    reviews, _ = mobile_api._do_mobile_reviews(rid, limit=20, offset=0)
    assert reviews["fetch_line"]["tone"] == "bad"


# ── Marketing (#36) ─────────────────────────────────────────────────────────

def test_metrics_line_is_amber_when_the_sync_fails(db_path, monkeypatch):
    rid = _rid(db_path)
    _set(db_path, rid, ig_token="ig")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO marketing_content_log (restaurant_id, topic, post_id, posted_at) VALUES (?,?,?,?)",
                 (rid, "Tacos", "p1", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    ok_at = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(df, "_metrics_sync", lambda r, c, x: {"last_ok_at": ok_at, "error": None})
    good = dh.metrics_line(models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    assert good["tone"] == "ok" and good["line"].startswith("Metrics synced ")
    monkeypatch.setattr(df, "_metrics_sync", lambda r, c, x: {"last_ok_at": ok_at, "error": "HTTP 400"})
    bad = dh.metrics_line(models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    assert bad["tone"] == "warn" and "failing" in bad["line"]
    assert dh.metrics_line(models.get_restaurant(_rid(db_path, name="No Social"), db_path=db_path),
                           db_path=db_path) is None


# ── Home (#17, #21, #37) ────────────────────────────────────────────────────

def test_home_cards_a_pos_whose_sales_stopped_and_embeds_data_health(db_path):
    rid = _rid(db_path, timezone="America/Chicago", module_labor=1, module_inventory=0, module_reviews=0)
    _set(db_path, rid, toast_restaurant_guid="guid-1", toast_last_synced=datetime.now(timezone.utc).isoformat())
    _day(db_path, rid, _local_today("America/Chicago") - timedelta(days=10))
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200
    card = [a for a in p["attention"] if a.get("key") == "pos_sync"]
    assert card and "stopped arriving" in card[0]["title"]
    assert isinstance(p["data_health"], dict) and isinstance(p["data_health"]["overall"]["pct"], int)
    assert p["data_health"]["overall"]["pct"] <= dh.ANY_STALE_CAP


def test_phone_home_says_freshness_is_unavailable_when_the_brief_fails(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(home_brief, "build_home_brief", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    payload, st = mobile_api._do_mobile_home(_user(rid))
    assert st == 200 and payload["freshness_unavailable"] is True and payload["freshness"] == []


def test_overnight_window_is_cut_in_the_restaurants_clock(db_path):
    """A review drafted 20 hours ago, local, is overnight work; read as UTC
    it was 27 hours old and dropped (DH4-15)."""
    rid = _rid(db_path, timezone=LA)
    local_now = datetime.now(ZoneInfo(LA)).replace(tzinfo=None)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, processed, "
                 "response_status, draft_response, fetched_at) VALUES (?,?,?,?,?,1,'drafted','Thanks!',?)",
                 (rid, "google", "o-1", 5, "great", (local_now - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()
    assert mobile_api._home_overnight(rid)["answered"] == 1


# ── the activity feed (#46) ─────────────────────────────────────────────────

def test_activity_dates_a_local_fetch_in_utc_and_is_bounded(db_path, monkeypatch):
    rid = _rid(db_path, timezone=LA, module_reviews=1)
    local_now = datetime.now(ZoneInfo(LA)).replace(tzinfo=None)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, processed, fetched_at) "
                 "VALUES (?,?,?,?,?,1,?)", (rid, "google", "a-1", 5, "great",
                                            (local_now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()
    feed = activity.build(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    at = next(e["at"] for e in feed["entries"] if e["kind"] == "analyzed")
    age = datetime.now(timezone.utc) - datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert timedelta(minutes=50) < age < timedelta(minutes=70)
    monkeypatch.setattr(activity, "_CACHE_MAX", 5)
    for i in range(40):
        activity._cache_put(("k", i), {})
    assert len(activity._CACHE) <= 5


def test_activity_says_review_checks_are_behind(db_path):
    rid = _rid(db_path, module_reviews=1)
    _set(db_path, rid, gmb_refresh_token="tok",
         last_fetched_at=_chi(datetime.now(timezone.utc) - timedelta(days=3)))
    feed = activity.build(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    texts = [w["text"] for w in feed["working"]]
    assert any(t.startswith("Review checks are behind — last check ") for t in texts)
    assert not any(t.startswith("Watching for new reviews") for t in texts)


# ── the morning brief (#12) ─────────────────────────────────────────────────

def _quiet_brief(monkeypatch):
    monkeypatch.setattr(morning_brief, "_day_context", lambda *a, **k: "")
    import business_intelligence as bi
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {})


def test_brief_leads_with_a_stopped_pos_and_holds_the_lines_on_it(db_path, monkeypatch):
    _quiet_brief(monkeypatch)
    import demand
    monkeypatch.setattr(demand, "yesterday_vs_typical",
                        lambda *a, **k: {"available": True, "actual": 4000, "typical": 4200, "pct": 5,
                                         "direction": "below", "weekday": "Wednesday", "off": False, "samples": 6})
    rid = _rid(db_path, timezone="America/Chicago", module_labor=1, module_inventory=0, module_reviews=0,
               dsr_enabled=0)
    _set(db_path, rid, toast_restaurant_guid="guid-1", toast_last_synced=datetime.now(timezone.utc).isoformat())
    stopped = _local_today("America/Chicago") - timedelta(days=10)
    _day(db_path, rid, stopped)
    brief = morning_brief.build(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    first = brief["lines"][0]
    assert first["key"] == "data_health" and first["tone"] == "bad"
    assert first["text"].startswith(f"⚠ Toast hasn't synced since {ce._mdy(stopped.isoformat())}")
    assert "yesterday's sales line is held" in first["text"]
    keys = [l["key"] for l in brief["lines"]]
    assert "yesterday" not in keys and "all_clear" not in keys
    assert brief["data_as_of"] and "pos" in brief["stale_sources"]
    html = morning_brief._email_html(brief, "Owner Cafe")
    assert f"Data as of {brief['data_as_of']}" in html and "out of date" in html


def test_all_clear_needs_the_watched_source_current(db_path, monkeypatch):
    _quiet_brief(monkeypatch)
    rid = _rid(db_path, module_reviews=1, module_labor=0, module_inventory=0, dsr_enabled=0)
    _set(db_path, rid, reviews_live=1, last_fetched_at=_chi(datetime.now(timezone.utc) - timedelta(days=3)))
    brief = morning_brief.build(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    keys = [l["key"] for l in brief["lines"]]
    assert "all_clear" not in keys and keys[0] == "data_health"
    assert "Reviews haven't been checked since" in brief["lines"][0]["text"]


def test_push_and_footer_say_how_current_the_data_is():
    long = {"key": "x", "tone": "action", "text": "A" * 400}
    pt = morning_brief.push_text({"lines": [long], "data_as_of": "9/22/26"}, "Owner Cafe")
    assert pt["body"].endswith(" Data as of 9/22/26.") and len(pt["body"]) <= 231
    foot = morning_brief.footer_source([{"key": "yesterday", "text": "x"}], "9/22/26", ["pos"])
    assert "Data as of 9/22/26." in foot and "POS sales is out of date" in foot
    assert morning_brief.footer_source([{"key": "data_health", "text": "⚠"}], "9/22/26", ["pos"]).startswith(
        "Nothing here is a measured figure.")


def test_prime_cost_is_stamped_with_what_it_rests_on(db_path, monkeypatch):
    _quiet_brief(monkeypatch)
    import food_cost_intelligence as fci
    monkeypatch.setattr(fci, "profitability_projection",
                        lambda *a, **k: {"available": True, "prime_cost_pct": 61.2, "prime_pct_delta": None,
                                         "claim_kind": "forecast", "labor_period": "9/1/26 to 9/14/26"})
    rid = _rid(db_path, timezone="America/Chicago", module_inventory=1, module_labor=0, module_reviews=0,
               dsr_enabled=0)
    yday = _local_today("America/Chicago") - timedelta(days=1)
    _day(db_path, rid, yday)
    brief = morning_brief.build(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    prime = next(l for l in brief["lines"] if l["key"] == "prime_cost")
    assert f"sales through {ce._mdy(yday.isoformat())}" in prime["text"]
    assert "labor share from 9/1/26 to 9/14/26" in prime["text"]


def test_money_line_carries_its_confidence_or_is_held(db_path, monkeypatch):
    monkeypatch.setattr(morning_brief, "_day_context", lambda *a, **k: "")
    import business_intelligence as bi
    money = {"ranked": [{"module": "labor", "key": "money:labor", "label": "Scheduling against target",
                         "monthly": 2400.0, "claim_kind": "opportunity", "basis": "gap above target"}]}
    monkeypatch.setattr(bi, "executive_brief", lambda *a, **k: {"money": money})
    rid = _rid(db_path, timezone="America/Chicago", module_labor=1, module_inventory=0, module_reviews=0,
               dsr_enabled=0)
    today = _local_today("America/Chicago")
    _day(db_path, rid, today - timedelta(days=1))
    brief = morning_brief.build(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path)
    line = next(l for l in brief["lines"] if l["key"] == "money")
    assert isinstance(line.get("confidence"), dict) and "data through" in morning_brief._conf_label(line)
    rid2 = _rid(db_path, name="Stale Labor", timezone="America/Chicago", module_labor=1, module_inventory=0,
                module_reviews=0, dsr_enabled=0)
    _day(db_path, rid2, today - timedelta(days=20))
    brief2 = morning_brief.build(rid2, restaurant=models.get_restaurant(rid2, db_path=db_path), db_path=db_path)
    assert "money" not in [l["key"] for l in brief2["lines"]]


# ── data_source_down (#18) ──────────────────────────────────────────────────

@pytest.fixture
def raised(monkeypatch):
    out = {"calls": [], "ok": True}

    def fake(rid, alert_type, sms, subject, **kw):
        out["calls"].append((alert_type, sms, subject, kw.get("lines")))
        return out["ok"]
    monkeypatch.setattr(notify, "raise_alert", fake)
    return out


def test_a_source_that_stopped_alerts_once_a_week_and_says_when_it_is_back(db_path, raised):
    rid = _rid(db_path, timezone="America/Chicago", module_labor=1, module_inventory=0, module_reviews=0)
    _set(db_path, rid, toast_restaurant_guid="guid-1", toast_last_synced=datetime.now(timezone.utc).isoformat())
    _day(db_path, rid, _local_today("America/Chicago") - timedelta(days=10))
    assert notify.data_source_pass(rid, db_path) == [("pos", "down")]
    alert_type, sms, subject, lines = raised["calls"][0]
    assert alert_type == "data_source_down" and "stopped updating" in subject
    assert "Labor" in sms and "Sync now" in sms
    assert notify.data_source_pass(rid, db_path) == []           # once per source per week
    _day(db_path, rid, _local_today("America/Chicago") - timedelta(days=1))
    assert notify.data_source_pass(rid, db_path) == [("pos", "restored")]
    assert raised["calls"][-1][0] == "data_source_restored"
    assert notify.data_source_pass(rid, db_path) == []


def test_a_refused_send_gives_its_claim_back(db_path, raised):
    raised["ok"] = False
    rid = _rid(db_path, timezone="America/Chicago", module_labor=1, module_inventory=0, module_reviews=0)
    _set(db_path, rid, toast_restaurant_guid="guid-1", toast_last_synced=datetime.now(timezone.utc).isoformat())
    _day(db_path, rid, _local_today("America/Chicago") - timedelta(days=10))
    notify.data_source_pass(rid, db_path)
    notify.data_source_pass(rid, db_path)
    assert len(raised["calls"]) == 2


def test_a_first_sync_that_never_arrives_alerts_after_the_grace(db_path, raised, monkeypatch):
    rid = _rid(db_path, timezone="America/Chicago", module_labor=1, module_inventory=0, module_reviews=0)
    _set(db_path, rid, toast_restaurant_guid="guid-1")
    assert notify.data_source_pass(rid, db_path) == []
    monkeypatch.setattr(notify, "_first_seen_hours", lambda *a: notify.FIRST_SYNC_GRACE_HOURS + 1)
    assert notify.data_source_pass(rid, db_path) == [("pos", "down")]
    assert "first sync" in raised["calls"][0][3][0]


def test_data_source_alerts_are_registered_and_inside_the_ceiling():
    import push
    for t in ("data_source_down", "data_source_restored"):
        assert t in push.PRIORITY and t in push.NOTIFICATION_MODULE and t in notify.ALERT_TAB
        assert t in client_api._NOTIFICATION_LABELS
        # Not exempt from the owner's cap and the hard ceiling.
        assert t not in models.NON_ALERT_TYPES and t not in notify.BRIEFING_ALWAYS
    ios = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Push/DeepLinkRouter.swift"), encoding="utf-8").read()
    assert '"data_source_down"' in ios and '"data_source_restored"' in ios
    import scheduler
    assert "check_data_source_alerts" in inspect.getsource(scheduler.run_daily_alert_checks)


# ── the rating alert (#38) ──────────────────────────────────────────────────

def test_rating_alert_needs_a_recent_google_rating(db_path, raised):
    rid = _rid(db_path, timezone="America/Chicago")
    _set(db_path, rid, alert_rating_threshold=1, alert_rating_floor=4.2, gbp_rating=3.9,
         gbp_rating_updated_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
    notify.check_daily_alerts(db_path=db_path)
    types = [c[0] for c in raised["calls"]]
    assert "rating_threshold" not in types and types.count("data_source_down") == 1
    assert "Can't read your Google rating" in raised["calls"][0][2]
    notify.check_daily_alerts(db_path=db_path)
    assert [c[0] for c in raised["calls"]].count("data_source_down") == 1      # said once
    _set(db_path, rid, gbp_rating_updated_at=datetime.now(timezone.utc).isoformat())
    notify.check_daily_alerts(db_path=db_path)
    assert "rating_threshold" in [c[0] for c in raised["calls"]]


# ── the monthly email (#38) ─────────────────────────────────────────────────

def test_monthly_email_does_not_call_an_unchecked_month_quiet(db_path, monkeypatch):
    import emails
    rid = _rid(db_path, timezone="America/Chicago", module_reviews=1, module_labor=1)
    _set(db_path, rid, gmb_refresh_token="tok", last_fetched_at=_chi(datetime.now(timezone.utc) - timedelta(days=20)))
    review_gap, pos_gap = emails._monthly_source_gaps(rid)
    assert review_gap and pos_gap is None
    sent = []
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.append(kw) or type("R", (), {"ok": True})())
    emails.send_monthly_summary_email("o@x.test", "Owner Cafe", "Sam", restaurant_id=rid, has_reviews=True)
    html = sent[0]["payload"]["html"]
    assert f"Reviews weren't checked after {review_gap}" in html and "No new reviews came in" not in html


def test_monthly_email_never_credits_a_failing_pos(db_path, monkeypatch):
    import emails
    rid = _rid(db_path, timezone="America/Chicago")
    _set(db_path, rid, toast_restaurant_guid="guid-1", toast_last_synced=datetime.now(timezone.utc).isoformat(),
         toast_sync_error="HTTP 401 unauthorized")
    _day(db_path, rid, _local_today("America/Chicago") - timedelta(days=9))
    assert emails._monthly_source_gaps(rid)[1] is not None


# ── the confidence impact on every K1 (#22) ─────────────────────────────────

def test_every_k1_carries_its_confidence_impact():
    ev = ce.evidence(n=60, kind="days")
    stale = ce.assemble(ev, None, {"pct": 30, "basis": "Counts from 9/2/26", "errors": [], "stalest": "inventory",
                                   "stalest_basis": "Counts from 9/2/26"})
    imp = stale["confidence_impact"]
    assert imp and imp["delta"] >= ce.IMPACT_MIN_DELTA and imp["when_current"] > stale["pct"]
    assert imp["blocked_by"] == "inventory"
    fresh = ce.assemble(ev, None, {"pct": 100, "basis": "current", "errors": []})
    assert fresh["confidence_impact"] is None
