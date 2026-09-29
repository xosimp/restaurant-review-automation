"""Memory fix round, integration wave (9/29/26): measured effects across the
workstreams, against M5's real event_memory (no stubs).

  #13 / PRED-26  every caller of event_memory's measurements matches its
                 real signatures and return shapes, and nothing acts on an
                 effect below its floor (`applies`).
  PRED-27        a campaign is measured ONCE: staffing, the forecast and the
                 campaign's own result read event_memory's measurement of
                 the campaign night itself.
  PRED-4         the published weeks' revenue record corrects only the
                 estimator it scored, and a week is never "explained by an
                 event" the projection already carried.
"""
import ast
import inspect
import json
import pathlib
import sys
from datetime import date, timedelta

import pytest

import event_memory
import models
from models import Restaurant, create_restaurant

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=db_path)
    yield


def _rid(name="Harbor Grill"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        timezone="America/Chicago", module_labor=1, module_marketing=1))


def _exec(sql, args=()):
    c = models.get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _outcome(rid, day, kind, label, lift, raw=None):
    _exec("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, lift_pct) "
          "VALUES (?,?,?,?,?,?,?)", (rid, day.isoformat(), day.strftime("%A"), kind, label, raw or label, lift))


def _event(rid, day, label):
    _exec("INSERT INTO demand_signals (restaurant_id, date, kind, label, source) VALUES (?,?,?,?,?)",
          (rid, day.isoformat(), "event", label, "owner"))


# ── #13 / PRED-26: the real shapes, and the floor ───────────────────────────

_MEASURES = {"measured_effect", "night_facts", "effects_for_day", "campaign_effect", "campaign_night"}

# Every caller outside event_memory, and how it honours the floor:
#   "checks"   — the function itself reads `applies` (or EFFECT_MIN_N)
#   "via X"    — it hands the effect to X, which checks
#   "floor inside" — effects_for_day applies the floor itself
#   "display"  — it says what a night was, and acts on no effect
CALLERS = {
    ("demand.py", "_apply_effects"): "floor inside",
    ("demand_signals.py", "measured_campaign_lift"): "checks",
    ("demand_signals.py", "_measured"): "via demand_signals.by_date",
    ("demand_signals.py", "_campaign_measured"): "via demand_signals.by_date",
    ("dsr/tomorrow.py", "_measured"): "via dsr.predictions._directed",
    ("forecast_log.py", "explained_by"): "checks",
    ("guest_marketing.py", "_campaign_night_effect"): "display",
    ("staffing_signals.py", "last_nights"): "display",
}


def _callers():
    found = set()
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".claude/", "venv/", ".venv/")) or rel == "event_memory.py" \
                or "/site-packages/" in rel:
            continue
        try:
            tree = ast.parse(p.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        stack = []

        class V(ast.NodeVisitor):
            def visit_FunctionDef(self, n):
                stack.append(n.name)
                self.generic_visit(n)
                stack.pop()
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, n):
                if isinstance(n.func, ast.Attribute) and n.func.attr in _MEASURES:
                    found.add((rel, stack[-1] if stack else "<module>"))
                self.generic_visit(n)
        V().visit(tree)
    return found


def _src(dotted):
    mod, fn = dotted.rsplit(".", 1)
    import importlib
    return inspect.getsource(getattr(importlib.import_module(mod), fn))


def test_every_caller_of_a_measured_effect_is_classified_and_honours_the_floor():
    assert _callers() == set(CALLERS), "a new caller of event_memory's measurements must be classified here"
    import importlib
    for (path, fn), how in CALLERS.items():
        mod = importlib.import_module(path[:-3].replace("/", "."))
        src = inspect.getsource(getattr(mod, fn))
        if how == "checks":
            assert "applies" in src or "EFFECT_MIN_N" in src, (path, fn)
        elif how.startswith("via "):
            assert "applies" in _src(how[4:]), (path, fn, how)
        elif how == "floor inside":
            assert 'e["applies"]' in inspect.getsource(event_memory.effects_for_day)
        else:
            assert "measured_lift_pct" not in src and "median_lift_pct" not in src, (path, fn)


def test_the_real_night_facts_carry_every_field_m3_and_m4_read():
    rid = _rid()
    day = date.today() - timedelta(days=9)
    _event(rid, day, "Cubs home game")
    for k, lift in enumerate((22.0, 20.0, 25.0)):
        _outcome(rid, day - timedelta(weeks=k + 1), "event", "cubs", lift)
    facts = event_memory.night_facts(rid, day)
    f = next(x for x in facts if x["label"] == "cubs")
    # forecast_log.explained_by reads these; staffing_signals.last_nights reads display/label.
    for key in ("kind", "label", "display", "measured_lift_pct", "n", "applies"):
        assert key in f, key
    assert f["applies"] is True and f["n"] == 3 and f["display"] == "Cubs home game"
    e = event_memory.measured_effect(rid, "cubs")
    for key in ("median_lift_pct", "n", "last", "applies", "direction", "basis"):
        assert key in e, key


def test_explained_by_on_the_real_module_needs_the_floor():
    import forecast_log
    from time_utils import mdy
    rid = _rid()
    day = date.today() - timedelta(days=9)
    _event(rid, day, "Cubs home game")
    _outcome(rid, day - timedelta(weeks=1), "event", "cubs", 22.0)
    end = forecast_log.period_end("revenue_week", day)
    assert forecast_log.explained_by(rid, "revenue_week", end) is None, "one night is not a pattern"
    for k, lift in enumerate((20.0, 25.0)):
        _outcome(rid, day - timedelta(weeks=k + 2), "event", "cubs", lift)
    assert forecast_log.explained_by(rid, "revenue_week", end) == f"Cubs home game on {mdy(day)}, +22% here"


# ── PRED-4: the week the projection already carried ─────────────────────────

def _sales(rid, weeks=9, per_day=1000.0):
    start = date.today() - timedelta(days=weeks * 7)
    for i in range(weeks * 7):
        d = start + timedelta(days=i)
        _exec("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost) "
              "VALUES (?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), per_day, per_day * 0.3))


def test_a_week_is_never_explained_by_an_event_its_projection_already_carried():
    import demand
    import forecast_log
    rid = _rid()
    _sales(rid)
    game = date.today() + timedelta(days=3)
    _event(rid, game, "Cubs home game")
    for k, lift in enumerate((22.0, 20.0, 25.0)):
        _outcome(rid, game - timedelta(weeks=k + 1), "event", "cubs", lift)
    week = [game - timedelta(days=game.weekday()) + timedelta(days=i) for i in range(7)]
    proj = demand.week_projection(rid, week)
    assert proj["modelled"] == {game.isoformat(): ["cubs"]}, "the forecast applied the measured game"
    frozen = demand.freeze_week_projection(rid, week[0])
    assert frozen.get("recorded", True) is not False
    c = models.get_conn()
    basis = c.execute("SELECT basis FROM forecast_log WHERE restaurant_id=? AND kind='revenue_week'",
                      (rid,)).fetchone()[0]
    c.close()
    assert json.loads(basis)["modelled"] == {game.isoformat(): ["cubs"]}
    end = forecast_log.period_end("revenue_week", game)
    assert forecast_log.explained_by(rid, "revenue_week", end, basis=basis) is None
    # A forecast that did not carry it (an older basis) is still explained by it.
    assert forecast_log.explained_by(rid, "revenue_week", end, basis=json.dumps({"days": []})) is not None
    assert "basis=r.get(\"basis\")" in inspect.getsource(forecast_log.score_due)


def test_the_revenue_record_corrects_only_the_estimator_it_scored():
    import schedule_economics as econ
    src = inspect.getsource(econ.projected_weekly_revenue)
    # The median path is returned raw; only the week_projection path is corrected.
    median_part = src[src.index("complete = sorted("):]
    assert "_corrected_by_record" not in median_part and "forecast_log" not in median_part
    assert "_corrected_by_record(restaurant_id, out, total" in src
    import demand
    assert "week_projection(" in inspect.getsource(demand.freeze_week_projection)


# ── PRED-27: one campaign measurement ───────────────────────────────────────

def test_staffing_the_forecast_and_the_campaigns_own_result_read_one_measurement():
    import demand
    import demand_signals
    import guest_marketing
    rid = _rid()
    _sales(rid)
    today = date.today()
    tuesday = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
    # Three campaign nights event_memory measured (+30, +25, +35 against a typical Tuesday).
    past = [tuesday - timedelta(weeks=w) for w in (2, 3, 4)]
    for d, lift in zip(past, (30.0, 25.0, 35.0)):
        _outcome(rid, d, "campaign", event_memory.CAMPAIGN_LABEL[0], lift, raw="Text to 300 guests to fill Tuesday")
    effect = event_memory.campaign_effect(rid)
    assert effect["applies"] and effect["median_lift_pct"] == 30.0
    # Staffing: the fill-a-night signal on the coming Tuesday reads it live.
    assert demand_signals.record_campaign(rid, "Tuesday", 412, today=tuesday - timedelta(days=1)) is True
    entry = demand_signals.by_date(rid, [tuesday.isoformat()])[tuesday.isoformat()]
    assert entry["lift_pct"] == 30 and entry["lift_source"] == "measured"
    assert demand_signals.measured_campaign_lift(rid)["lift_pct"] == 30
    # The forecast: the same campaign effect on the same night.
    fc = demand.forecast_day(rid, tuesday)
    assert fc["available"] and fc["effect_pct"] == 30.0
    assert [e["kind"] for e in fc["effects"]] == ["campaign"] and fc["effects"][0]["lift_pct"] == 30.0
    # Every campaign night is the one label, however the signal was worded.
    flags = event_memory.flags_for(rid, [tuesday], known_before=True)[tuesday.isoformat()]
    assert [(f["kind"], f["label"], f["owner_lift_pct"]) for f in flags] == [
        ("campaign", event_memory.CAMPAIGN_LABEL[0], None)]
    # The campaign's own result: its target night, as event_memory measured it.
    sent_on = past[0] - timedelta(days=1)
    _exec("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, target_day, created_at, status) "
          "VALUES (?,?,?,?,?,?)", (rid, "Come in Tuesday!", 300, "Tuesday", f"{sent_on.isoformat()} 10:00:00",
                                   "done"))
    hist = guest_marketing.campaign_history(rid)
    assert hist[0]["night_effect"]["lift_pct"] == 30.0 and hist[0]["night_effect"]["date"] == past[0].isoformat()
    assert "+30% against a typical Tuesday" in hist[0]["night_effect"]["text"]
    assert "before and after, not proof" in hist[0]["night_effect"]["text"]


def test_the_outcome_trackers_diluted_change_is_not_the_campaign_lift():
    import demand_signals
    src = inspect.getsource(demand_signals.measured_campaign_lift)
    assert "campaign_effect" in src and "recommendation_outcomes" not in src
    assert "lift = None" in inspect.getsource(demand_signals.record_marketing)
