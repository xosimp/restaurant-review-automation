"""Schedule fix round 10/11/26, workstream G2 — the labor standard keeps its
pre-go-live cap for good and is fitted against service outcomes (blind
re-audit LEARNING-8, LOGIC-21)."""
from datetime import date, timedelta

import pytest

import models
import requirement_provenance as rp
import staffing_model as sm


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


LIVE = "2026-07-01"


def test_the_cap_outlives_the_window():
    """sim_cap.py: capped at 20.5 while pre-go-live nights are in the 8-week
    window, raw 30-32 after."""
    early = {"2026-06-10": 20.0, "2026-06-17": 21.0, "2026-07-08": 30.0, "2026-07-15": 32.0}
    late = {"2026-09-02": 30.0, "2026-09-09": 32.0, "2026-09-16": 31.0}
    assert rp.cap_unvalidated(early, LIVE, set(), part="night")["2026-07-08"] == 20.5
    assert set(rp.cap_unvalidated(late, LIVE, set(), part="night", reference=20.5).values()) == {20.5}
    # Validated nights keep their own figure.
    ok = {("2026-09-09", "night")}
    assert rp.cap_unvalidated(late, LIVE, ok, part="night", reference=20.5)["2026-09-09"] == 32.0
    # No reference at all: Cavnar AI's own unvalidated nights do not set it.
    assert rp.cap_unvalidated(late, LIVE, ok, part="night") == {"2026-09-09": 32.0}


def _slots(days, guests, people):
    work = {d: {"guests": {1140: guests}, "tickets": {1140: guests // 2}, "bar tickets": {}} for d in days}
    ppl = {d: {"server": {1140: people, 1110: people, 1170: people}} for d in days}
    return work, ppl


def _fridays(start, n):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(weeks=i)).isoformat() for i in range(n)]


def test_measure_keeps_the_pre_go_live_reference_and_caps_against_it_later():
    rid = models.create_restaurant(models.Restaurant(name="Std Co", owner_email="s@x.test", module_labor=1))
    pre = _fridays("2026-05-08", 7)
    work, ppl = _slots(pre, 40, 4)                                # 20 guests per server-hour
    first = sm.measure(rid, work=work, people=ppl, live=LIVE, ok_pairs=set(), overrides={}, prior={},
                       outcomes={})
    assert first["families"]["server"]["parts"]["night"]["std"] == 20.0
    # The window's own pre-go-live nights are measured... (all pre here)
    # ...and once the window has moved past go-live the Cavnar-era lean
    # nights (32 per server-hour) are held to the stored reference.
    stored = sm.load_references(rid)
    assert stored[("server", "night")]["reference"] == 20.0 and stored[("server", "night")]["days"] == 7
    post = _fridays("2026-09-04", 7)
    work2, ppl2 = _slots(post, 64, 4)
    later = sm.measure(rid, work=work2, people=ppl2, live=LIVE, ok_pairs=set(), overrides={}, prior={},
                       outcomes={})
    night = later["families"]["server"]["parts"]["night"]
    assert night["std"] == 20.0 and night["capped"] == 7


def test_a_window_straddling_go_live_stores_the_reference():
    rid = models.create_restaurant(models.Restaurant(name="Std Co 2", owner_email="t@x.test", module_labor=1))
    days = _fridays("2026-05-08", 4) + _fridays("2026-07-03", 4)
    work, ppl = _slots(days, 40, 4)
    sm.measure(rid, work=work, people=ppl, live=LIVE, ok_pairs=set(), overrides={}, prior={}, outcomes={})
    ref = sm.load_references(rid)[("server", "night")]
    assert ref["reference"] == 20.0 and ref["days"] == 4 and ref["go_live"] == LIVE


def test_outcome_standard_finds_where_service_slows():
    pairs = [(10 + i, 30.0) for i in range(6)] + [(18 + i, 31.0) for i in range(3)] + [(24 + i, 45.0) for i in range(3)]
    o = sm.outcome_standard(pairs)
    assert o["limited"] and o["optimal"] < 24
    held = sm.outcome_standard([(10 + i, 30.0 + (i % 2)) for i in range(12)])
    assert not held["limited"] and held["optimal"] == held["terciles"][-1][0]
    assert sm.outcome_standard([(10, 30.0)] * 4) == {}


def test_a_standard_service_cannot_hold_is_lowered_to_the_level_it_holds_at():
    days = _fridays("2026-05-08", 12)
    work, ppl = {}, {}
    outcomes = {}
    for i, d in enumerate(days):
        g = [30, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70, 74][i]
        w, p = _slots([d], g, 2)
        work.update(w)
        ppl.update(p)
        # Tables turn slower once one server carries more than ~50 guests an hour.
        outcomes[d] = {"ticket": {1140: 60.0 if g * 2 / 2 <= 50 else 80.0}}
    model = sm._build(work, ppl, sorted(days), None, set(), {}, {}, outcomes=outcomes)
    night = model["families"]["server"]["parts"]["night"]
    assert night["source"] == "outcome" and night["std"] < night["outcome"]["habit"]
    assert "service holds to about" in night["text"]
