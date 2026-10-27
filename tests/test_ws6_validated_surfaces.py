"""Audit #25 A07-06 — at every model call site that validates in a helper
(tests/test_rv_adoption_source.py's VALIDATED_IN), a stubbed model answer
stating a figure nothing traces to is never shown as fact.

The adoption test proves each call site CALLS the validator; it would pass
a refactor that kept the call and dropped the branch that acts on its
verdict. Here each site runs end to end with the model stubbed: an honest
answer is served clean (so the test is not vacuous), and the same answer
with an invented figure gets the surface's fallback —
  * the night's narrative (unattended, dsr): the line is dropped, or the
    summary held back — the owner never reads the figure;
  * a diagnosis (owner-facing, review_diagnosis / food_diagnosis): the
    validator WITHHOLDS (response_validation: a caveat and no controls) —
    served with "Some figures here aren't in your data: $…", its actions
    withheld and its confidence capped low, the figure listed for the
    card's unverified mark; and a person it was never handed refuses the
    read whole, yesterday's standing.
"""
import json
import types
from datetime import timedelta

import pytest

import ai_utils
import models
from tests.test_rv_adoption_source import VALIDATED_IN

FAKE = "9,999"


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import review_intelligence as ri
    import food_cost_intelligence as fci
    monkeypatch.setattr(ri, "DB_PATH", db_path)
    monkeypatch.setattr(fci, "DB_PATH", db_path, raising=False)
    import data_health
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "proceed"})
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    ai_utils.reset_breaker()
    yield db_path
    ai_utils.reset_breaker()


def _answer(monkeypatch, body):
    """Every create_with_retry returns `body` (a dict, as JSON) — the model."""
    sent = []

    def create(client, **kw):
        sent.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(body))],
                                     stop_reason="end_turn",
                                     usage=types.SimpleNamespace(input_tokens=900, output_tokens=300,
                                                                 cache_creation_input_tokens=0,
                                                                 cache_read_input_tokens=0))
    monkeypatch.setattr(ai_utils, "create_with_retry", create)
    return sent


def _rid(name="Harbor Grill"):
    return models.create_restaurant(models.Restaurant(name=name, owner_email="o@harbor.test"))


# ── food_cost_intelligence.diagnose ─────────────────────────────────────────

_DRIVERS = {"available": True, "complete": True, "degraded_sources": [], "reason": None,
            "drivers": [{"label": "Salmon waste above tolerance", "item": "Salmon", "kind": "waste",
                         "dollars_monthly": 640.0, "confidence": "high", "difficulty": "low",
                         "evidence": "Salmon waste ran $640 a month over tolerance.",
                         "if_ignored": "The waste keeps costing $640 a month."}],
            "total_monthly": 640.0, "total_monthly_deduplicated": 640.0, "totals_by_kind": {"estimate": 640.0},
            "total_kinds": ["estimate"], "total_basis": "no ingredient appears in more than one driver",
            "min_driver_dollars": 50, "basis": "priced from usage"}


def _food(monkeypatch, db_path, cause, headline="Salmon waste leads food cost"):
    import food_cost_intelligence as fci
    monkeypatch.setattr(fci, "cost_drivers", lambda *a, **k: json.loads(json.dumps(_DRIVERS)))
    rid = _rid()
    sent = _answer(monkeypatch, {"headline": headline, "cause": cause,
                                 "alternative_cause": "The Salmon par is set for a busier week.",
                                 "what_would_confirm": "Two weeks of Salmon counts against sales.",
                                 "operational_evidence": [], "confidence": "medium",
                                 "recommended_action": "Cut the Tuesday Salmon order.",
                                 "expected_outcome": "If the Tuesday order is cut, Salmon waste should fall."})
    try:
        fci.diagnose(rid, db_path=db_path, force=True)
    except ValueError:
        pass                     # refused whole: nothing new is stored
    assert sent, "the model was never asked"
    return fci.get_diagnosis(rid, db_path=db_path, include_stale=True)


def _withheld(served):
    """The diagnosis surfaces' fallback for an untraced figure."""
    v = served.get("validation") or {}
    assert v.get("verdict") == "withhold" and v.get("controls") is False, v
    assert any(FAKE in c and "aren't in your data" in c for c in v.get("caveats") or []), v
    assert served["confidence"] == "low" and f"${FAKE}" in (served.get("unsupported_figures") or [])


def test_food_cost_diagnosis_serves_an_honest_read(monkeypatch, db_path):
    served = _food(monkeypatch, db_path, "Salmon is over-ordered for weekday demand, about $640 a month over tolerance.")
    assert served and "Salmon is over-ordered" in served["cause"]
    assert (served.get("validation") or {}).get("controls") is not False and not served.get("unsupported_figures")


def test_food_cost_diagnosis_never_serves_an_invented_figure_as_fact(monkeypatch, db_path):
    served = _food(monkeypatch, db_path, f"Salmon is over-ordered for weekday demand, costing ${FAKE} a month.")
    _withheld(served)


def test_food_cost_diagnosis_naming_a_stranger_is_refused_whole(monkeypatch, db_path):
    served = _food(monkeypatch, db_path, "Chef Luis over-portions the Salmon on weekdays.")
    assert served is None, "a read naming someone it was never handed was stored"


# ── review_intelligence.diagnose ────────────────────────────────────────────

def _reviews(rid, n=4):
    from time_utils import restaurant_now_by_id
    day = restaurant_now_by_id(rid).date() - timedelta(days=3)
    conn = models.get_conn()
    try:
        ids = []
        for i in range(n):
            cur = conn.execute(
                "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                "fetched_at, sentiment, categories, processed, summary) VALUES (?, 'google', ?, ?, 2, ?, ?, ?, "
                "'negative', ?, 1, ?)",
                (rid, f"ext-{i}", f"Guest {i}", "We waited 40 minutes for our food on Friday night.",
                 day.isoformat(), day.isoformat() + " 12:00:00", json.dumps(["service"]), "A long wait."))
            ids.append(cur.lastrowid)
        conn.commit()
        return ids
    finally:
        conn.close()


def _review(monkeypatch, db_path, cause):
    import review_intelligence as ri
    rid = _rid()
    ids = _reviews(rid)
    sent = _answer(monkeypatch, {"cause": cause, "alternative_cause": "The kitchen was short on Fridays.",
                                 "evidence_review_ids": ids[:3], "what_would_confirm": "Friday ticket times.",
                                 "operational_evidence": [], "confidence": "medium",
                                 "recommended_action": "Add a runner on Friday dinner."})
    ri.diagnose(rid, db_path=db_path, force=True)
    assert sent, "the model was never asked"
    return ri.get_diagnoses(rid, db_path=db_path, include_stale=True)


def test_review_diagnosis_serves_an_honest_read(monkeypatch, db_path):
    served = _review(monkeypatch, db_path, "Friday dinner service is slow: guests waited 40 minutes for food.")
    assert served and "Friday dinner service is slow" in served[0]["cause"]
    assert not served[0].get("unsupported_figures")


def test_review_diagnosis_never_serves_an_invented_figure_as_fact(monkeypatch, db_path):
    served = _review(monkeypatch, db_path,
                     f"Friday dinner service is slow and is costing ${FAKE} a month in lost covers.")
    _withheld(served[0])


# ── dsr/narrative.run ───────────────────────────────────────────────────────

def test_the_nights_narrative_never_serves_an_invented_figure(monkeypatch, db_path):
    from tests import test_dsr_narrative as T
    rest = models.get_restaurant(models.create_restaurant(
        models.Restaurant(name="Simple EJ's", owner_email="erik@example.com"), db_path=db_path), db_path=db_path)
    monkeypatch.delenv("DSR_NARRATIVE_MODEL", raising=False)
    honest, _c = T._run(monkeypatch, rest, db_path, T.strong_night(), T.strong_reply())
    assert honest["ok"] and honest["narrative"]["executive_summary"]["text"]
    # An invented figure on a line: the line is dropped, the rest goes out.
    bad = T.strong_reply()
    bad["went_well"].append(T._it(f"Comps came to ${FAKE} tonight.", "sales.comps"))
    out, _c = T._run(monkeypatch, rest, db_path, T.strong_night(), bad)
    assert out["ok"]
    shown = [i["text"] for i in out["narrative"]["went_well"]]
    assert len(shown) == len(T.strong_reply()["went_well"]) and not any(FAKE in t for t in shown)
    assert any(d["field"].startswith("went_well") for d in out["narrative"]["verification"]["dropped"])
    # In the opening: the summary is held back, nothing goes out.
    bad = T.strong_reply()
    bad["executive_summary"] = T._it(f"Saturday did ${FAKE} net. Labor ran 24.2%.", "sales.net", "labor.pct")
    out, _c = T._run(monkeypatch, rest, db_path, T.strong_night(), bad)
    assert out["ok"] is False and out["narrative"] is None and "held back" in out["reason"]


# ── every VALIDATED_IN site has its behaviour test here ─────────────────────

COVERED = {
    ("dsr/narrative.py", "run"): test_the_nights_narrative_never_serves_an_invented_figure,
    ("review_intelligence.py", "diagnose"): test_review_diagnosis_never_serves_an_invented_figure_as_fact,
    ("food_cost_intelligence.py", "diagnose"): test_food_cost_diagnosis_never_serves_an_invented_figure_as_fact,
}


def test_every_validated_in_site_has_a_behaviour_test():
    assert set(VALIDATED_IN) <= set(COVERED), f"add a stubbed-answer test for {set(VALIDATED_IN) - set(COVERED)}"
