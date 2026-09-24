"""Data sources, freshness and cold start — confidence re-audit round 2,
group S (blind re-audits B3, B6, B1, B4). Each test replays an auditor's
probe (scratchpad blind3/p1–p10, blind6/probe_disconnected_pos.py,
probe_pos_states.py) or pins the rule its fix introduced, and fails on the
code before the fix.

The owner's rule (binding): every percentage is a real computed measure;
below a floor the surface shows "—" and what is needed."""
import inspect
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import auth
import client_api
import confidence_engine as ce
import data_freshness
import home_brief
import mobile_api
import models
import pos_health
from auth import init_auth
from models import Restaurant, create_restaurant, save_client_data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
PROVIDERS = {"toast": {"toast_restaurant_guid": "g"}, "square": {"square_access_token": "t"},
             "clover": {"clover_api_token": "t"}, "rpower": {"rpower_token": "t", "rpower_store_mid": "m"}}


def _src(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


@pytest.fixture
def db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, home_brief):
        monkeypatch.setattr(mod, "get_conn", redirect)
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if f.startswith(ROOT) and callable(getattr(mod, "get_conn", None)) and mod is not data_freshness:
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    from models import init_email_log
    init_email_log(db_path=db_path)
    import ai_utils
    c = real(db_path); c.executescript(ai_utils._USAGE_TABLE_SQL); c.commit(); c.close()
    home_brief.invalidate()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model called")))
    return db_path


def _rid(db, name="Fresh Co", **kw):
    """A restaurant, every other column set on the row itself (create_restaurant
    writes only its own subset)."""
    rid = create_restaurant(Restaurant(name=name, owner_email="f@x.test", owner_name="Pat Owner"), db_path=db)
    cols = dict(module_reviews=1, module_labor=1)
    cols.update(kw)
    _set(db, rid, **cols)
    return rid


def _set(db, rid, **cols):
    c = models.get_conn(db)
    c.execute(f"UPDATE restaurants SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), rid))
    c.commit(); c.close()


def _row(db, rid):
    c = models.get_conn(db)
    r = dict(c.execute("SELECT * FROM restaurants WHERE id=?", (rid,)).fetchone())
    c.close()
    return r


def _x(db, sql, *a):
    c = models.get_conn(db); c.execute(sql, a); c.commit(); c.close()


def _user(rid):
    return {"id": None, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "owner",
            "is_admin": 0, "email": "o@x.test"}


def _shifts_csv(n_days, sales_gap=0, end=None, extra=()):
    end = end or (date.today() - timedelta(days=1))
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    for i in range(n_days):
        d = end - timedelta(days=i)
        sales = "" if i < sales_gap else str(2400 + (900 if d.weekday() >= 4 else 0) + (i % 5) * 40)
        for e, (role, st, en) in enumerate((("Server", "10:00", "16:00"), ("Server", "16:00", "22:00"),
                                             ("Cook", "09:00", "17:00"), ("Cook", "15:00", "23:00"),
                                             ("Busser", "17:00", "22:00"))):
            h = int(en[:2]) - int(st[:2])
            lines.append(f"{d.isoformat()},{d.strftime('%A')},Emp{e},{role},{st},{en},{h},{h},{sales},")
    lines.extend(extra)
    return "\n".join(lines)


def _upload(db, rid, csv):
    save_client_data(rid, "shifts", csv, source="upload", db_path=db)
    from labor import full_history_by_day
    models.save_labor_daily_history(rid, full_history_by_day(rid))


# ── S1 (CRITICAL): the sample week is never a diagnosis ────────────────────

def test_s1_sample_week_diagnosis_is_unavailable_and_never_reaches_the_ledger(db):
    """B3#1 / probe p4: a brand-new restaurant's Labor tab showed the bundled
    sample week's cause at 25% with answer buttons and wrote a rec_instances
    row."""
    import labor
    rid = _rid(db)
    a = labor.analyse_shifts_for_restaurant(rid)
    assert a["is_live"] is False and a.get("total_sales")          # the sample week is there
    d = labor.diagnose(a)
    assert d["available"] is False and d.get("sample") is True and not d.get("cause")
    assert labor.diagnosis_evidence_input(a)["sample"] is True
    out = client_api._labor_diagnosis_safe(rid)
    assert out["available"] is False and not out.get("rec_key") and not out.get("answerable")
    c = models.get_conn(db)
    assert c.execute("SELECT COUNT(*) FROM rec_instances WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM rec_events WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    c.close()
    # …and the weekly plan's cause anchors never carry the sample week's cause.
    import strategy_jobs
    assert not any("Sunday" in (x or "") for x in strategy_jobs._plan_cause_anchors(rid, db_path=db))


def test_s1_a_live_restaurant_still_gets_its_diagnosis(db):
    import labor
    rid = _rid(db)
    _upload(db, rid, _shifts_csv(28))
    d = labor.diagnose(labor.analyse_shifts_for_restaurant(rid))
    assert d["available"] is True and "sample" not in d["evidence_input"]


# ── S2: connected means credentials; disconnects clear the stamp ───────────

def test_s2_a_leftover_stamp_is_not_a_connection():
    """B6#1 / probe_disconnected_pos: a disconnect that left the stamp read
    as connected, decayed to 0% and zeroed a full-evidence labor card."""
    for name in PROVIDERS:
        row = {"id": 999999, "timezone": "America/Chicago",
               f"{name}_last_synced": (NOW - timedelta(days=20)).isoformat()}
        ph = pos_health.pos_sync_state(row, now=NOW)
        assert ph["connected"] is False and ph["state"] == "not_connected", name
        st = data_freshness._pos(row, None, None, NOW, None)
        assert st["pct"] is None and st["state"] == "not_connected", name
        ev = ce.evidence(n=28, kind="trading_days", coverage=1.0, basis="28 trading days")
        acc = ce.accuracy({"source": "own", "measured": 8, "improved": 6})
        assert ce.assemble(ev, acc, ce.freshness([st]))["pct"] > 0, name


def test_s2_every_disconnect_route_clears_the_sync_stamp():
    import rpower_routes
    for fn in (rpower_routes.disconnect_rpower, rpower_routes.rpower_disconnect_client):
        assert '"rpower_last_synced": None' in inspect.getsource(fn)
    assert '"toast_last_synced": None' in inspect.getsource(mobile_api.mobile_disconnect_toast)


def test_s2_the_freshest_credentialed_provider_is_read():
    """B3#16 / probe p1 "dual": a dead Toast stamp beat a fresh RPOWER."""
    row = {"toast_restaurant_guid": "g", "toast_last_synced": (NOW - timedelta(days=30)).isoformat(),
           "rpower_token": "t", "rpower_last_synced": NOW.replace(tzinfo=None).isoformat(timespec="seconds")}
    s = pos_health.pos_sync_state(row, now=NOW)
    assert s["provider"] == "rpower" and s["state"] == "current"


# ── S3: the POS is dated by its data; sales ride with every sales module ───

def test_s3_sales_stopping_under_a_fresh_sync_is_seen(db):
    """B3#3 / probe p7: Toast synced today, the last 10 days carry no sales —
    pos read 100, and the labor diagnosis had no staleness caution."""
    rid = _rid(db, toast_restaurant_guid="g", toast_last_synced=datetime.now(timezone.utc).isoformat())
    import pos
    pos.save_synced_shifts(rid, _shifts_csv(28, sales_gap=10), "toast")
    for m in ("labor", "schedule", "inventory", "food", "food_cost", "dsr"):
        assert "sales" in data_freshness.MODULE_SOURCES[m], m
    r = _row(db, rid)
    p = data_freshness.source_state(r, "pos", db_path=db)
    assert p["pct"] < ce.STALE_BELOW and "sales through" in p["basis"]
    dg = client_api._labor_diagnosis_safe(rid)
    cd = dg["confidence_detail"]
    assert cd["pct"] <= ce.STALE_CAP and cd["caution"] and "out of date" in cd["caution"]


def test_s3_a_never_synced_pos_does_not_zero_uploaded_labor(db):
    """B1 H1a: connecting Toast before its first sync dropped every labor
    card built from uploaded shifts to 0%."""
    rid = _rid(db, toast_restaurant_guid="g")
    _upload(db, rid, _shifts_csv(28))
    st = data_freshness.source_state(_row(db, rid), "pos", db_path=db)
    assert st["pct"] is None and st["state"] == "unknown" and "never synced" in st["basis"]
    dg = client_api._labor_diagnosis_safe(rid)
    if dg.get("available"):
        assert dg["confidence_detail"]["pct"] and dg["confidence_detail"]["pct"] > 0


# ── S4: an erroring source is strictly below the stale threshold ───────────

def test_s4_an_erroring_source_always_trips_the_stale_cap():
    """B3#2 / probe p1: a 401 on a fresh stamp read exactly 50 — the engine's
    `f < 50` never fired, so a broken POS read like a healthy one."""
    assert 100 * data_freshness.ERROR_CEILING < ce.STALE_BELOW
    for name, cols in PROVIDERS.items():
        row = dict(cols, id=999999, **{f"{name}_last_synced": NOW.isoformat(), f"{name}_sync_error": "401"})
        st = data_freshness._pos(row, None, None, NOW, None)
        assert st["error"] and st["pct"] < ce.STALE_BELOW, name
        ev = ce.evidence(n=28, kind="trading_days", coverage=1.0, basis="28 trading days")
        conf = ce.assemble(ev, ce.accuracy(None), ce.freshness([st]))
        assert conf["pct"] <= ce.STALE_CAP and "out of date" in conf["caution"], name


# ── S5: future dates are typos, refused and never fresh ────────────────────

def test_s5_a_future_row_is_refused_at_upload_and_never_anchors_the_window(db):
    """B3#4 / probe p9: one 2027 row passed validation and turned the
    analysis into a one-day window at 6.9%, 100% fresh."""
    import labor
    typo = date.today() - timedelta(days=1)
    typo = typo.replace(year=typo.year + 1)
    row = f"{typo.isoformat()},{typo.strftime('%A')},Emp9,Server,10:00,18:00,8,8,3000,"
    csv = _shifts_csv(28, extra=(row,))
    rows, errs = labor.validate_shifts_csv(csv)
    assert errs and "after today" in errs[0]
    rid = _rid(db)
    _upload(db, rid, csv)                           # stored anyway (an old file)
    a = labor.analyse_shifts_for_restaurant(rid)
    assert a["date_range"]["end"] == (date.today() - timedelta(days=1)).isoformat()
    assert a["period_days"] == 28
    st = data_freshness.source_state(_row(db, rid), "labor", db_path=db)
    assert st["as_of_iso"] == (date.today() - timedelta(days=1)).isoformat()


def test_s5_future_stamps_read_unknown_never_current(db):
    """B3#14 / probe p1, p3: a POS stamp 5 days ahead and a competitor stamp
    40 days ahead both read 100%."""
    row = {"id": 999999, "toast_restaurant_guid": "g", "toast_last_synced": (NOW + timedelta(days=5)).isoformat()}
    st = data_freshness._pos(row, None, None, NOW, None)
    assert st["state"] == "unknown" and st["pct"] == 0
    rid = _rid(db, competitor_updated_at=(NOW + timedelta(days=40)).isoformat())
    st = data_freshness.source_state(_row(db, rid), "competitor", db_path=db, now=NOW)
    assert st["state"] == "unknown" and st["pct"] == 0


def test_s5_a_sync_drops_future_rows():
    src = inspect.getsource(__import__("pos").save_synced_shifts)
    assert "drop_future_shifts(" in src


# ── S6: marketing — never synced is an error, an unreadable expiry unknown ─

def test_s6_marketing_never_synced_and_unparseable_expiry(db):
    """B3#5 / probe p3: both read 100%."""
    rid = _rid(db, module_marketing=1, ig_token="t", ig_token_expires=(NOW + timedelta(days=30)).isoformat())
    _x(db, "INSERT INTO marketing_content_log (restaurant_id, content_type, post_id, created_at) "
           "VALUES (?, 'post', 'p1', ?)", rid, (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    st = data_freshness.source_state(_row(db, rid), "marketing", db_path=db, now=NOW)
    assert st["pct"] < ce.STALE_BELOW and "never synced" in st["error"]
    _set(db, rid, ig_token_expires="not a date")
    st = data_freshness.source_state(_row(db, rid), "marketing", db_path=db, now=NOW,
                                     context={"metrics_sync": {"last_ok_at": NOW.isoformat()}})
    assert st["state"] == "unknown" and st["pct"] == 0


# ── S7: a stale forecast is never today's weather ──────────────────────────

def test_s7_stale_weather_is_never_told_as_today(monkeypatch):
    """B3#6 / probe p3: a 70-hour fallback forecast was the morning brief's
    and pre-shift's weather, and could trim a shift for rain."""
    import morning_brief
    import weather
    stale = [{"date": "2026-09-24", "high_f": 88, "short_forecast": "Sunny", "precip_pct": 70, "stale": True}]
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: stale)
    assert "88" not in morning_brief._day_context(object(), date(2026, 9, 24))
    fresh = [dict(stale[0], stale=False)]
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: fresh)
    assert "88" in morning_brief._day_context(object(), date(2026, 9, 24))
    assert 'not wx[0].get("stale")' in _src("preshift.py")
    assert 'if _w.get("stale"):' in _src("schedule_engine.py")
    assert "weather" in data_freshness.MODULE_SOURCES["schedule"]
    assert "weather" in data_freshness.MODULE_SOURCES["demand"]


# ── S8: one Places "sampled" helper on every review surface ────────────────

def test_s8_places_only_reviews_carry_the_sampled_flag_everywhere():
    """B3#7 / probe p8: Home 74 → 70/80, Reviews page 100 → 70/88 for the
    same diagnosis."""
    places = {"reviews_live": 1, "google_place_id": "ChIJx"}
    assert data_freshness.review_evidence_flags(places) == ("sampled",)
    assert data_freshness.review_evidence_flags(dict(places, gmb_refresh_token="t")) == ()
    import rec_trust
    dg = {"confidence": "high", "operational_evidence": []}
    fl = data_freshness.review_evidence_flags(places)
    home = ce.evidence(**rec_trust.diagnosis_evidence(dg, 12, "reviews", "home", flags=fl))
    page = ce.evidence(**rec_trust.diagnosis_evidence(dg, 12, "reviews", "page", flags=fl))
    assert home["pct"] == page["pct"] < 100
    for path in ("home_brief.py", "review_intelligence.py", "client_api.py", "business_intelligence.py"):
        assert "review_evidence_flags(" in _src(path), path


# ── S9: day-1 floors ───────────────────────────────────────────────────────

def test_s9_one_day_of_shifts_makes_no_labor_claim_on_home(db):
    """B3#8 / probe p6: one day of shifts — the tile said "5.8 pts over
    target", the brief "Labor 35.8% against a 30% target" in red."""
    rid = _rid(db, module_inventory=0, module_marketing=0)
    _upload(db, rid, _shifts_csv(1))
    p, st = home_brief.build_home_brief(_user(rid), fresh=True, present=False)
    assert st == 200
    tile = next(s for s in p["snapshot"] if s["key"] == "labor")
    assert tile["value"] == "—" and tile["below_floor"] is True and tile["state"] == "neutral"
    assert "over target" not in tile["interpretation"]
    line = next(l for l in p["brief"]["lines"] if l["module"] == "labor")
    assert "against a" not in line["text"] and line["tone"] == "neutral"
    assert not any(w["key"] == "labor_on_target" for w in p["wins"])


def test_s9_the_group_view_uses_the_location_floors():
    src = inspect.getsource(home_brief._location_record)
    assert 'labor.get("period_days", 0) >= _MIN_DAYS_G' in src
    assert 'rs.get("n_prev") or 0) >= REVIEW_MOVE_MIN_N' in src


def test_s9_running_well_is_never_said_over_stale_data(db):
    """B4 M4: a green "Running well" on a month-old labor file."""
    rid = _rid(db, module_reviews=0, module_inventory=0, module_marketing=0, labor_target_pct=60)
    _upload(db, rid, _shifts_csv(28, end=date.today() - timedelta(days=30)))
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True, present=False)
    assert "Running well" not in p["brief"]["headline"]
    assert p["monitoring"]["stale"] >= 1 and p["monitoring"]["all_clear"] is False
    assert not any(w["key"] == "labor_on_target" for w in p["wins"])


# ── S10: one POS freshness rule ────────────────────────────────────────────

def test_s10_pos_health_and_the_registry_agree_at_every_age():
    """B6#5 / probe_pos_states: at 4 days pos_health said stale while the
    registry read 64% aging."""
    for age in (1.0, 1.6, 2.0, 2.5, 3.1, 4.0, 5.0, 6.0, 9.0):
        r = {"id": 999999, "toast_restaurant_guid": "g", "toast_last_synced": (NOW - timedelta(days=age)).isoformat(),
             "timezone": "America/Chicago"}
        ph = pos_health.pos_sync_state(r, now=NOW)
        df = data_freshness._pos(r, None, None, NOW, None)
        assert ph["state"] == df["state"] == ce.state(df["pct"]), age
        assert ph["pct"] == df["pct"], age


def test_s10_home_review_nudge_and_portfolio_read_the_registry():
    """B3#9: Home's reviews_stale and the portfolio used a 3-day cut."""
    for src in (inspect.getsource(home_brief._location_signal), _src("home_brief.py")):
        assert "review_fetch_state(" in src
    assert "age > 3" not in inspect.getsource(home_brief._location_signal)
    r = {"id": 1, "gmb_refresh_token": "x",
         "last_fetched_at": (NOW - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")}
    assert data_freshness.review_fetch_state(r, now=NOW)["state"] == "stale"


def test_s10_admin_fleet_reads_the_pos_state():
    import admin_ops
    src = inspect.getsource(admin_ops.integrations)
    assert 'i.get("sync_state") == "stale"' in src
    assert '_pos.get("state") in ("current", "aging")' in _src("admin_ops.py")


# ── S11: missing sales days penalised once ─────────────────────────────────

def test_s11_missing_sales_days_are_not_also_a_freshness_penalty(db):
    """B3#10 / probe p7: the same 10 missing days gave evidence 41 AND
    freshness 64."""
    rid = _rid(db)
    for i in range(28):
        d = (date.today() - timedelta(days=1 + i)).isoformat()
        _x(db, "INSERT INTO labor_daily_history (restaurant_id,date,labor_cost,sales,total_hours) VALUES (?,?,?,?,?)",
           rid, d, 900, None if i < 10 else 3000, 40)
    st = data_freshness.source_state(_row(db, rid), "labor", db_path=db)
    assert st["pct"] == 100 and "18 of 28 days carry sales" in st["basis"]


# ── S12: a provisional daily report is never current ───────────────────────

def test_s12_a_provisional_dsr_is_held_under_current(db):
    """B3#12 / probe p3: a provisional report for yesterday read 100."""
    rid = _rid(db)
    today = datetime.now(timezone.utc)
    _x(db, "INSERT INTO dsr_reports (restaurant_id,business_date,status,provisional,finalized_at) VALUES (?,?,?,?,?)",
       rid, (today - timedelta(days=1)).date().isoformat(), "delivered", 1, today.isoformat())
    st = data_freshness.source_state(_row(db, rid), "dsr", db_path=db)
    assert st["pct"] is not None and st["pct"] < ce.CURRENT_AT and st["state"] != "current"
    assert "provisional" in st["basis"]


# ── S13: every scored surface passes sources ───────────────────────────────

def test_s13_ask_tools_map_to_sources():
    """B3#13 / probe p5: outcomes, goals, decisions, platform and the snapshot
    mapped to no source; the demand forecast missed the weather."""
    for tool in ("read_outcomes", "read_goals", "read_decisions", "read_platform_intelligence",
                 "read_business_snapshot"):
        assert data_freshness.sources_for_tools([tool]), tool
    assert "weather" in data_freshness.sources_for_tools(["read_demand_forecast"], ["labor"])
    assert "sources_for_tools(tools_used, modules)" in _src("ask_cavnar.py")


def test_s13_campaign_diagnosis_measures_freshness(db, monkeypatch):
    import guest_marketing
    import rec_trust
    seen = {}
    real = rec_trust.assess

    def spy(*a, **k):
        seen["sources"] = k.get("sources")
        return real(*a, **k)
    monkeypatch.setattr(rec_trust, "assess", spy)
    guest_marketing._with_confidence(1, {}, {"n": 3, "kind": "campaigns", "flags": (), "basis": "x"}, db_path=db)
    assert seen["sources"] and "pos" in seen["sources"]


# ── S14: empty pulls, wording, partial YoY, the passed clock ───────────────

@pytest.mark.parametrize("prov", ["toast", "square", "clover"])
def test_s14_an_empty_pull_writes_the_sync_error(db, monkeypatch, prov):
    """B3#15: Toast, Square and Clover returned early without writing it."""
    mod = __import__(prov)
    rid = _rid(db, **PROVIDERS[prov])
    monkeypatch.setattr(mod, "build_shifts_csv", lambda *a, **k: "")
    assert mod.sync_to_db(rid)["ok"] is False
    assert _row(db, rid)[f"{prov}_sync_error"]


def test_s14_reporter_labor_window_and_yoy_whole_week():
    src = _src("reporter.py")
    assert "of revenue this week" not in src and "days of shifts " in src
    assert "len(yoy_sales) == len(yoy_context)" in _src("labor.py")


def test_s14_review_misses_count_against_the_passed_clock():
    """B3#20: _reviews counted missed slots against the real clock."""
    then = datetime(2026, 1, 10, 18, 0, tzinfo=timezone.utc)
    r = {"id": 1, "gmb_refresh_token": "x", "last_fetched_at": (then - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}
    st = data_freshness.review_fetch_state(r, now=then)
    assert st["state"] == "current" and not st["error"]


# ── S15: the public status page names no restaurant ────────────────────────

def test_s15_status_page_counts_locations_never_names(db, monkeypatch):
    """B6#6: "POS not synced in 3+ days: {names}" on a public page."""
    import status_manager as sm
    from auth import create_user
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: models.get_conn(db), raising=False)
    rid = _rid(db, name="Secret Bistro", rpower_token="t", rpower_store_mid="m",
               rpower_last_synced=NOW.replace(tzinfo=None).isoformat(timespec="seconds"),
               rpower_sync_error="401 Unauthorized")
    create_user(rid, "u1", "u1@x.test", "Passw0rd!xyz", db_path=db)
    sm.seed_default_services()
    sm._check_labor_analytics()
    c = models.get_conn(db)
    row = c.execute("SELECT status, message FROM service_status WHERE service_key='labor_analytics'").fetchone()
    c.close()
    assert row["status"] == "degraded" and "Secret Bistro" not in row["message"] and "1 location" in row["message"]


# ── S16: an admin save keeps Places-only review fetching on ────────────────

def test_s16_admin_settings_post_the_stored_reviews_live():
    src = _src("templates/client_settings.html")
    assert "reviews_live:    {{ 1 if (restaurant.gmb_refresh_token or restaurant.reviews_live) else 0 }}" in src
    from jinja2 import Environment
    tpl = Environment().from_string("{{ 1 if (restaurant.gmb_refresh_token or restaurant.reviews_live) else 0 }}")
    assert tpl.render(restaurant={"gmb_refresh_token": None, "reviews_live": 1}) == "1"


# ── S17: small integrity items ─────────────────────────────────────────────

def test_s17_reporter_labor_history_needs_sales():
    assert "WHERE restaurant_id=? AND total_sales > 0" in _src("reporter.py")


def test_s17_forecast_log_get_conn_resolves_at_call_time(monkeypatch):
    import forecast_log
    calls = []
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: calls.append(a) or "conn")
    assert forecast_log.get_conn(forecast_log.DB_PATH) == "conn" and calls == [()]


def test_s17_freshness_readers_pass_db_path(monkeypatch, db):
    import metrics
    import fetcher
    seen = {}
    monkeypatch.setattr(metrics, "coverage", lambda *a, **k: seen.setdefault("metrics", k) and None)
    monkeypatch.setattr(fetcher, "places_coverage", lambda *a, **k: seen.setdefault("fetcher", k) and {})
    rid = _rid(db, reviews_live=1, google_place_id="ChIJx",
               last_fetched_at=(datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
    _x(db, "INSERT INTO labor_daily_history (restaurant_id,date,labor_cost,sales,total_hours) VALUES (?,?,?,?,?)",
       rid, (date.today() - timedelta(days=1)).isoformat(), 900, 3000, 40)
    data_freshness.source_state(_row(db, rid), "sales", db_path=db)
    data_freshness.source_state(_row(db, rid), "reviews", db_path=db)
    assert seen["metrics"].get("db_path") == db and seen["fetcher"].get("db_path") == db


def test_s17_portfolio_reads_last_fetched_at_as_chicago_local():
    """B6 low: _ts read the Chicago-local stamp as server-local."""
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)
    ct = (now - timedelta(hours=2)).astimezone(ZoneInfo("America/Chicago")).replace(tzinfo=None)
    st = data_freshness.review_fetch_state({"id": 1, "gmb_refresh_token": "x",
                                            "last_fetched_at": ct.isoformat(timespec="seconds")}, now=now)
    assert abs(st["age_days"] - 2 / 24.0) < 0.01
