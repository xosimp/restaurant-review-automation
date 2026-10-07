"""Memory fix round M5, event_memory (memory audit 9/29/26: CROSS-4,
QUALITY-6, LOOPS-4, FORGET-9).

What each night teaches — how a game, a holiday, rain, the 1st of the month
or a campaign moved THIS restaurant's sales — is measured after the night is
final and kept, and read back: by the demand forecast (behind a sample
floor), by the owner's listed events (measured once they recur), by the
nightly report's predictions (only in the measured direction), by
memory_context, and by the other workstreams (measured_effect, night_facts).
"""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import event_memory as em
import models
from models import Restaurant, create_restaurant

TUE = date(2026, 9, 29)          # a Tuesday; game nights below avoid the 1st and the 15th (paydays)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import pos
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    yield


def _rid(name="Wrigley Tap", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    if cols:
        conn = models.get_conn()
        conn.execute(f"UPDATE restaurants SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _sales(rid, d, net, provider="rpower", final=1):
    conn = models.get_conn()
    conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                 "labor_pct, final, provider) VALUES (?,?,?,?,?,?,?,?)",
                 (rid, d.isoformat(), d.strftime("%A"), net, net * 0.3, 30.0, final, provider))
    conn.commit()
    conn.close()


def _influence(rid, d, text):
    conn = models.get_conn()
    conn.execute("INSERT INTO close_outs (restaurant_id, business_date, influence) VALUES (?,?,?)",
                 (rid, d.isoformat(), text))
    conn.commit()
    conn.close()


def _history(rid, games, weeks=16, base=4000.0, game_net=5000.0, until=TUE):
    """`weeks` Tuesdays before `until`, $4,000 plain and $5,000 on the ones
    in `games` (week offsets), each game with a closer's "Cubs game" note."""
    for k in range(1, weeks + 1):
        d = until - timedelta(weeks=k)
        _sales(rid, d, game_net if k in games else base)
        if k in games:
            _influence(rid, d, "Cubs home game" if k % 2 else "cubs vs cards")


# ── labels ─────────────────────────────────────────────────────────────────

def test_labels_are_normalised_and_a_note_naming_two_things_is_two():
    assert em.normalise_label("Cubs home game!") == "cubs"
    assert em.normalise_label("Homecoming") == "homecoming"
    assert em.split_labels("Cubs game + rain") == ["cubs", "rain"]
    assert em.split_labels("rainy, Bears game") == ["rain", "bears"]
    assert em.split_labels("NOTHING") == [] and em.split_labels("") == []


# ── recording a night ──────────────────────────────────────────────────────

def test_a_night_is_measured_against_its_ordinary_same_weekdays():
    rid = _rid()
    _history(rid, games={3, 6, 9, 12})
    night = TUE - timedelta(weeks=3)                            # 9/8/26
    got = em.record_night(rid, night)
    assert got["recorded"] == 1 and got["labels"] == ["cubs"]
    # The baseline is the eight Tuesdays before it WITHOUT the other flagged
    # nights — the games of 8/18 and 7/28 and the payday of 9/1: five plain
    # $4,000 Tuesdays, so the game ran +25%.
    conn = models.get_conn()
    row = dict(conn.execute("SELECT * FROM event_outcomes WHERE restaurant_id=?", (rid,)).fetchone())
    conn.close()
    assert row["kind"] == "influence" and row["baseline"] == 4000.0 and row["baseline_n"] == 5
    assert row["lift_pct"] == 25.0 and row["basis"] == "dsr_net" and row["labor_pct"] == 30.0
    # Re-recording the night replaces its rows, never adds a second.
    em.record_night(rid, night)
    conn = models.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM event_outcomes WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
    conn.close()


def test_a_label_applies_once_it_has_recurred_and_is_matched_by_its_words():
    rid = _rid()
    _history(rid, games={3, 6, 9})
    for k in (3, 6, 9):
        em.record_night(rid, TUE - timedelta(weeks=k))
    e = em.measured_effect(rid, "Cubs home opener")        # "cubs opener" → only nights naming both
    assert e is None
    e = em.measured_effect(rid, "Cubs")
    assert e["n"] == 3 and e["median_lift_pct"] == 25.0 and e["applies"] is True and e["direction"] == "up"
    assert e["last"] == TUE - timedelta(weeks=3) and "measured 3 times" in e["basis"]
    assert "2026-" not in e["basis"]                           # M/D/YY only
    # The per-label summaries are kept (event_effects), one row per label as
    # written: "Cubs home game" twice and "cubs vs cards" once.
    conn = models.get_conn()
    rows = {r["label"]: dict(r) for r in conn.execute("SELECT * FROM event_effects WHERE restaurant_id=?", (rid,))}
    conn.close()
    assert rows["cubs"]["n"] == 2 and rows["cubs"]["median_lift_pct"] == 25.0 and rows["cubs"]["direction"] == "up"
    assert rows["cubs cards"]["n"] == 1


def test_two_nights_are_not_yet_a_pattern():
    rid = _rid()
    _history(rid, games={3, 6})
    for k in (3, 6):
        em.record_night(rid, TUE - timedelta(weeks=k))
    e = em.measured_effect(rid, "cubs")
    assert e["n"] == 2 and e["applies"] is False


def test_rain_is_what_was_observed_and_a_holiday_and_payday_are_flagged():
    rid = _rid()
    # 10/1/26 is a Thursday and the 1st; 10/31/26 Halloween is a Saturday.
    first = date(2026, 10, 1)
    flags = em.flags_for(rid, [first, date(2026, 10, 31)])
    assert {f["kind"] for f in flags["2026-10-01"]} == {"payday"}
    assert [f["raw"] for f in flags["2026-10-31"] if f["kind"] == "holiday"] == ["Halloween"]
    conn = models.get_conn()
    conn.execute("INSERT INTO weather_daily (restaurant_id, date, rain, conditions, high_f) VALUES (?,?,?,?,?)",
                 (rid, "2026-10-01", 1, "Light Rain", 58))
    conn.commit()
    conn.close()
    kinds = {f["kind"] for f in em.flags_for(rid, [first])["2026-10-01"]}
    assert kinds == {"payday", "rain"}
    # A forecast may only use what is known before the night.
    assert {f["kind"] for f in em.flags_for(rid, [first], known_before=True)["2026-10-01"]} == {"payday"}


def test_the_super_bowl_is_never_measured_on_a_guessed_date():
    assert em._holiday(date(2026, 2, 8)) is None


def test_a_toast_night_is_measured_on_the_reports_basis_only(monkeypatch):
    import pos
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("toast", object()))
    rid = _rid("Toast Tap")
    night = TUE - timedelta(weeks=1)
    for k in range(1, 9):
        _sales(rid, night - timedelta(weeks=k), 4200.0, provider="toast")     # POS totals: another basis
    _sales(rid, night, 5200.0, provider="toast")
    _influence(rid, night, "Cubs")
    got = em.measure_night(rid, night)
    # The night itself is a POS total too: measured against POS totals only.
    assert got["basis"] == "pos_daily_total" and got["baseline"] == 4200.0
    conn = models.get_conn()
    conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                 "VALUES (?,?,?,?,?,?)", (rid, night.isoformat(), "sales.net", 5000.0, "ready", "toast"))
    conn.commit()
    conn.close()
    # Now the night is the report's own net, and no POS total is set beside it.
    got = em.measure_night(rid, night)
    assert got.get("lift_pct") is None and "same basis" in got["reason"]


# ── read back: the forecast, listed events, predictions ────────────────────

def test_the_forecast_applies_a_measured_effect_known_before_the_night():
    import demand
    import demand_signals
    rid = _rid()
    _history(rid, games={3, 6, 9, 12})
    for k in (3, 6, 9, 12):
        em.record_night(rid, TUE - timedelta(weeks=k))
    plain = demand.forecast_day(rid, TUE)
    assert "effects" not in plain
    demand_signals.save(rid, [{"date": TUE.isoformat(), "kind": "event", "label": "Cubs home game"}])
    fc = demand.forecast_day(rid, TUE)
    assert fc["base_sales"] == plain["typical_sales"] and fc["effect_pct"] == 25.0
    assert fc["typical_sales"] == round(plain["typical_sales"] * 1.25, 2)
    assert fc["effects"][0]["n"] == 4 and "measured 4 times" in fc["effect_basis"]
    # "A typical Tuesday" is still the plain median.
    assert demand.forecast_day(rid, TUE, effects=False)["typical_sales"] == plain["typical_sales"]


def test_a_listed_event_takes_its_measured_lift_and_an_owner_figure_stands_beside_it():
    import demand_signals as ds
    rid = _rid()
    _history(rid, games={3, 6, 9})
    for k in (3, 6, 9):
        em.record_night(rid, TUE - timedelta(weeks=k))
    ds.save(rid, [{"date": "2026-10-06", "kind": "event", "label": "Cubs game"},
                  {"date": "2026-10-13", "kind": "event", "label": "Cubs home game", "lift_pct": 40},
                  {"date": "2026-10-20", "kind": "event", "label": "Homecoming"}])
    by = ds.by_date(rid, ["2026-10-06", "2026-10-13", "2026-10-20"])
    assert by["2026-10-06"]["lift_pct"] == 25 and by["2026-10-06"]["lift_source"] == "measured"
    assert by["2026-10-06"]["measured_n"] == 3 and "assumed" not in by["2026-10-06"]
    assert by["2026-10-13"]["lift_pct"] == 40 and "lift_source" not in by["2026-10-13"]
    assert by["2026-10-20"]["assumed"] is True                     # no record: still an assumption
    block = ds.prompt_block(by, ["2026-10-06", "2026-10-13", "2026-10-20"])
    assert "measured median over 3 past nights" in block
    # The figure itself is said once, in the date's SHIFT REQUIREMENTS row
    # (C1, PR-8): here, what the night is and how sure its figure is.
    assert "+25%" not in block and "40%" not in block and "(the owner's own figure)" in block
    assert "ASSUMED" in block


def test_predictions_state_rain_only_in_the_direction_measured_here():
    from dsr import predictions
    fc = {"available": True, "typical_sales": 6000.0, "base_sales": 6000.0, "low": 5000.0, "high": 7000.0,
          "samples": 8, "weekday": "Friday"}
    wet = {"precip_pct": 80}
    assert [p["key"] for p in predictions.build(fc, weather=wet)] == ["sales_range"]
    up = {"applies": True, "direction": "up", "median_lift_pct": 12.0, "n": 5}
    preds = predictions.build(fc, weather=wet, effects={"rain": up})
    rain = next(p for p in preds if p["key"] == "rain")
    assert rain["op"] == "gt" and "above a usual Friday" in rain["text"] and "measured 5 times" in rain["basis"]
    thin = dict(up, applies=False)
    assert "rain" not in {p["key"] for p in predictions.build(fc, weather=wet, effects={"rain": thin})}
    ev = [{"label": "Homecoming", "kind": "event"}]
    assert "event" not in {p["key"] for p in predictions.build(fc, events=ev)}
    down = {"applies": True, "direction": "down", "median_lift_pct": -15.0, "n": 3}
    p = next(p for p in predictions.build(fc, events=ev, effects={"events": {"Homecoming": down}})
             if p["key"] == "event")
    assert p["op"] == "lt" and "pull sales below" in p["text"]


# ── read back: memory_context and the other workstreams ─────────────────────

def test_memory_lines_name_the_measured_effect_for_the_dates_in_play_fenced():
    import ai_guard
    import demand_signals as ds
    import memory_context as mc
    rid = _rid()
    _history(rid, games={3, 6, 9})
    for k in (3, 6, 9):
        em.record_night(rid, TUE - timedelta(weeks=k))
    ds.save(rid, [{"date": "2026-10-06", "kind": "event", "label": "Cubs game"}])
    block = mc.memory_context(rid, "schedule", now=datetime(2026, 10, 1, 9, 0))
    assert "events" in block.sections and "10/6/26" in block.text and "2026-10-06" not in block.text
    assert "median 25% above" in block.text and "measured 3 times" in block.text
    assert ai_guard.UNTRUSTED_OPEN in block.text
    lines = em.memory_lines(mc.MemoryRequest(restaurant_id=rid, surface="brief", now=datetime(2026, 10, 6, 7, 0)))
    assert len(lines) == 1 and lines[0]["trusted"] is False and lines[0]["subject"] == "event:cubs"


def test_night_facts_carries_each_labels_record_and_the_observed_weather():
    rid = _rid()
    _history(rid, games={3, 6, 9})
    for k in (3, 6, 9):
        em.record_night(rid, TUE - timedelta(weeks=k))
    night = TUE - timedelta(weeks=3)
    conn = models.get_conn()
    conn.execute("INSERT INTO weather_daily (restaurant_id, date, rain, conditions, high_f, low_f) VALUES (?,?,?,?,?,?)",
                 (rid, night.isoformat(), 0, "Partly Cloudy", 71, 55))
    conn.commit()
    conn.close()
    facts = em.night_facts(rid, night)
    cubs = next(f for f in facts if f["label"] == "cubs")
    assert cubs["measured_lift_pct"] == 25.0 and cubs["n"] == 3 and cubs["this_night_lift_pct"] == 25.0
    w = next(f for f in facts if f["kind"] == "weather_observed")
    assert w["high_f"] == 71 and w["rained_in_service"] == 0
    assert em.night_facts(rid, date(2027, 1, 5)) == []


# ── the weather that happened (NWS observations, stubbed) ───────────────────

def _obs(at_utc, temp_c, text="", codes=(), precip=None):
    return {"properties": {"timestamp": at_utc, "textDescription": text,
                           "temperature": {"value": temp_c}, "precipitationLastHour": {"value": precip},
                           "presentWeather": [{"weather": c} for c in codes]}}


def test_the_days_observations_are_summarised_on_the_local_day(monkeypatch):
    import weather

    class _R:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload
    feats = [_obs("2026-09-28T17:00:00+00:00", 20.0, "Cloudy"),                     # noon CDT
             _obs("2026-09-28T23:00:00+00:00", 18.0, "Light Rain", ("rain",), 1.5),   # 6pm CDT, in service
             _obs("2026-09-29T02:00:00+00:00", 15.0, "Rain", ("rain",), 2.0),         # 9pm CDT
             _obs("2026-09-29T09:00:00+00:00", 10.0, "Clear")]                         # 4am CDT the 29th
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["timeout"] = timeout
        return _R({"features": feats})
    monkeypatch.setattr(weather.requests, "get", fake_get)
    obs, failure = weather.fetch_observations("KMDW", datetime(2026, 9, 28, 5, tzinfo=timezone.utc),
                                              datetime(2026, 9, 30, 5, tzinfo=timezone.utc))
    assert failure is None and seen["timeout"] and len(obs) == 4
    from zoneinfo import ZoneInfo
    day = weather.summarise_day(obs, ZoneInfo("America/Chicago"), date(2026, 9, 28))
    assert day["rain"] == 1 and day["wet_hours"] == 2 and day["high_f"] == 68.0 and day["low_f"] == 59.0
    assert day["precip_in"] == round(3.5 / 25.4, 2) and day["conditions"] in ("Light Rain", "Rain", "Cloudy")


def test_record_weather_needs_coordinates_and_keeps_the_station(monkeypatch):
    import weather
    rid = _rid("Coord Co")
    r = models.get_restaurant(rid)
    assert em.record_weather(r, [date(2026, 9, 28)])["reason"] == "no coordinates on file"
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET latitude=41.88, longitude=-87.63 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    calls = {"station": 0}

    def station(lat, lon):
        calls["station"] += 1
        return "KMDW", None
    obs = [{"at": datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc), "temp_f": 64.0, "precip_mm": 1.0,
            "wet": True, "text": "Rain"}]
    monkeypatch.setattr(weather, "observation_station", station)
    monkeypatch.setattr(weather, "fetch_observations", lambda s, a, b: (obs, None))
    got = em.record_weather(models.get_restaurant(rid), [date(2026, 9, 28)])
    assert got["recorded"] == 1
    row = em.observed_weather(rid, date(2026, 9, 28))
    assert row["rain"] == 1 and row["station"] == "KMDW" and row["kind"] == "measured"
    em.record_weather(models.get_restaurant(rid), [date(2026, 9, 27), date(2026, 9, 28)])
    assert calls["station"] == 1                                   # asked once, kept on the rows


# ── the nightly job ────────────────────────────────────────────────────────

def test_the_job_returns_the_standard_counts_and_skips_demo_accounts(monkeypatch):
    import weather
    monkeypatch.setattr(weather, "observation_station", lambda lat, lon: (None, "not_covered"))
    live = _rid("Live Co")
    demo = _rid("Demo Co", is_demo=1)
    _history(live, games={2, 5, 9}, until=date.today() + timedelta(days=(1 - date.today().weekday()) % 7))
    _history(demo, games={2, 5, 9}, until=date.today() + timedelta(days=(1 - date.today().weekday()) % 7))
    out = em.run_event_memory()
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out)
    assert out["ok"] >= 1 and out["skipped"] >= 1 and out["failed"] == 0
    conn = models.get_conn()
    n_live = conn.execute("SELECT COUNT(DISTINCT business_date) FROM event_outcomes WHERE restaurant_id=? "
                          "AND label LIKE 'cubs%'", (live,)).fetchone()[0]
    n_demo = conn.execute("SELECT COUNT(*) FROM event_outcomes WHERE restaurant_id=?", (demo,)).fetchone()[0]
    conn.close()
    assert n_live == 3 and n_demo == 0                          # a demo's seeded nights teach nothing


def test_the_nightly_report_records_its_night_when_it_finishes():
    import inspect
    from dsr import pipeline
    # _advance hands the finished night to _conclude since the DSR narrative
    # can wait on a batch (AI cost audit 10/7/26 #20); the night is recorded there.
    src = inspect.getsource(pipeline._advance) + inspect.getsource(pipeline._conclude)
    assert "_conclude(" in inspect.getsource(pipeline._advance)
    assert "_remember(restaurant, day, db)" in inspect.getsource(pipeline._conclude)
    assert "event_memory.record_night" in inspect.getsource(pipeline._remember)


def test_the_job_is_registered_and_run_by_the_loop():
    import inspect
    import jobs_registry
    import scheduler
    assert jobs_registry.JOBS["event_memory"]["target"] == ("event_memory", "run_event_memory")
    assert jobs_registry.JOBS["event_memory"]["sends"] is False
    assert 'run_job("event_memory"' in inspect.getsource(scheduler.scheduler_loop)


def test_an_import_reopens_the_history_backfill():
    from dsr import store
    rid = _rid("Import Co")
    em._backfill_cursor(rid, value="2025-09-01")
    store.import_history(rid, [{"date": "2025-10-31", "gross": 9000.0, "net": 8200.0}])
    assert em._backfill_cursor(rid) is None


def test_the_report_shows_the_weather_that_happened_beside_the_forecast():
    import dsr
    from dsr import access, store
    rid = _rid("Weather Report Co")
    day = date(2026, 9, 22)
    r = store.create_report(rid, day)
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics={"net": 5000.0}))
    store.save_block(r["id"], "intel", dsr.block(dsr.READY, source="cavnar", metrics={"weather_precip_pct": 70.0},
                                                 detail={"weather": {"basis": "forecast", "summary": "Rain likely"}}))
    store.set_stage(r["id"], "final")
    owner = {"id": 1, "role": "client", "is_admin": 0}
    out = access.render(store.get_report_by_id(r["id"]), owner)
    assert "observed" not in out["facts"]["blocks"]["intel"]["detail"]["weather"]
    conn = models.get_conn()
    conn.execute("INSERT INTO weather_daily (restaurant_id, date, high_f, low_f, rain, conditions, station) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, day.isoformat(), 64.0, 51.0, 0, "Mostly Cloudy", "KMDW"))
    conn.commit()
    conn.close()
    w = access.render(store.get_report_by_id(r["id"]), owner)["facts"]["blocks"]["intel"]["detail"]["weather"]
    assert w["basis"] == "forecast" and w["summary"] == "Rain likely"          # the forecast stays a forecast
    assert w["observed"]["rained_in_service"] == 0 and w["observed"]["summary"] == "Mostly Cloudy · high 64°"
    assert "observed by the nearest National Weather Service station" in w["observed"]["basis"]


def test_the_brief_says_a_measured_effect_instead_of_calling_the_day_typical(monkeypatch):
    import demand
    import morning_brief
    rid = _rid("Brief Effect Co")
    monkeypatch.setattr(demand, "forecast_day", lambda r, day=None, db_path=None: {
        "available": True, "weekday": "Tuesday", "typical_sales": 5000.0, "base_sales": 4000.0, "samples": 8,
        "low": 4400.0, "high": 5600.0, "effects": [{"label": "cubs", "display": "Cubs home game", "lift_pct": 25.0,
                                                    "n": 4, "kind": "event"}]})
    monkeypatch.setattr(morning_brief, "_day_context", lambda r, d: "")
    brief = morning_brief.build(rid, today=TUE)
    (today,) = [l for l in brief["lines"] if l["key"] == "today"]
    assert today["text"].startswith("Today: about $5,000 — a typical Tuesday is $4,000, with Cubs home game +25% "
                                    "(measured 4 times here)")
    assert "looks like a typical" not in today["text"]


def test_the_events_screen_carries_what_the_nights_taught():
    from flask import Flask
    import demand_signals as ds
    import strategy_routes
    rid = _rid()
    _history(rid, games={3, 6, 9})
    for k in (3, 6, 9):
        em.record_night(rid, TUE - timedelta(weeks=k))
    ds.save(rid, [{"date": "2026-10-06", "kind": "event", "label": "Cubs game"},
                  {"date": "2026-10-07", "kind": "reservations", "covers": 40}])
    s = em.summaries(rid)
    assert s[0]["applies"] is False                               # one wording each: 2 + 1 nights, below the floor
    assert {x["label"] for x in s} == {"cubs", "cubs cards"} and "measured 2 times" in s[0]["text"]
    app = Flask(__name__)
    with app.test_request_context("/labor/demand-signals?start=2026-10-01&end=2026-10-31"):
        body, status = strategy_routes._do_demand_signals_get({"restaurant_id": rid, "role": "owner", "is_admin": 1})
    assert status == 200 and body["what_nights_teach"] == s
    ev = next(x for x in body["signals"] if x["kind"] == "event")
    assert ev["measured"]["n"] == 3 and ev["measured"]["applies"] is True and "10/" not in ev["measured"]["last"]
    assert "measured" not in next(x for x in body["signals"] if x["kind"] == "reservations")


def test_each_recorded_night_keeps_who_worked_it_and_the_effect_says_so():
    """Owner, 9/30/26: it poured all day - does it learn how many servers
    were on? Each recorded night keeps the people punched in by role and
    their hours, and a label's measured effect says what stood on its nights."""
    import shift_facts
    rid = _rid()
    _history(rid, games={3, 6, 9})
    for k, servers in ((3, 4), (6, 5), (9, 4)):
        d = TUE - timedelta(weeks=k)
        rows = [{"date": d.isoformat(), "day": d.strftime("%A"), "employee": f"Server {i}", "role": "Server",
                 "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "6",
                 "sales": "5000", "schedule_known": "0"} for i in range(servers)]
        rows.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": "Cook A", "role": "Kitchen",
                     "shift_start": "15:00", "shift_end": "23:00", "scheduled_hours": "8", "actual_hours": "8",
                     "sales": "5000", "schedule_known": "0"})
        shift_facts.ingest(rid, rows, "rpower")
        em.record_night(rid, d)
    conn = models.get_conn()
    r = conn.execute("SELECT headcount, headcount_json, labor_hours FROM event_outcomes WHERE restaurant_id=? "
                     "AND business_date=?", (rid, (TUE - timedelta(weeks=6)).isoformat())).fetchone()
    conn.close()
    assert r["headcount"] == 6 and json.loads(r["headcount_json"]) == {"Kitchen": 1, "Server": 5}
    assert r["labor_hours"] == 38.0
    e = em.measured_effect(rid, "Cubs")
    assert e["staffing"]["median_headcount"] == 5 and e["staffing"]["by_role"] == {"Kitchen": 1, "Server": 4}
    assert "a median 5 people worked (4 Server, 1 Kitchen)" in e["basis"]
