"""Ask Cavnar through the Response Validation Layer (workstream A).

Before: Ask's `_finish` only flagged — the answer text went out unchanged
with figures / causes / names listed in meta, and "You saved $1,240",
"definitely why", "Unlike Gia Mia down the street" and "I've sent the order"
all reached the owner as written (NS1 C2, NS2, NS3 R2, NS6 A5 / B). Now the
engine is the one check: its first pass gives the flags the K1 confidence
is measured from, its second applies the rewrites and drops with that
confidence, and meta carries the findings and the structured verdict —
which both Ask routes (web + mobile, streamed and not) pass through.
"""
import json
import types

import pytest

import ask_cavnar
import models
from models import Restaurant, create_restaurant, get_restaurant

SNAPSHOT = ("RESTAURANT: Probe Bistro\nLABOR: 31.4% of sales against a 30% target. "
            "REVIEWS: likely cause: Friday dinner is short a line cook.\n")
FOOD_TOOL = json.dumps({"is_live": True, "food": {"recoverable_monthly": 1240, "waste_week": 310}})


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))


def _rid(db_path, name="Probe Bistro"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name[:4].lower()}@x.com", module_reviews=1,
                                        module_labor=1, module_inventory=1), db_path=db_path)


def _finish(answer, rid, corpus=(SNAPSHOT, FOOD_TOOL), tools_used=("read_food_cost",), actions_done=()):
    return ask_cavnar._finish(answer, list(corpus), list(tools_used), [], "standard", rid,
                              actions_done=actions_done)


def test_an_opportunity_called_saved_is_rewritten_in_the_answer(db_path):
    rid = _rid(db_path)
    answer, meta = _finish("You saved $1,240 a month on food last month.", rid)
    assert "saved $1,240" not in answer and "not money saved" in answer
    assert meta["validation"]["version"] and "F4" in meta["validation"]["codes"]


def test_certainty_and_a_cause_are_lowered_to_what_the_data_supports(db_path):
    rid = _rid(db_path)
    answer, meta = _finish("Short staffing on Friday dinner is definitely why ratings fell.", rid)
    assert "definitely" not in answer
    assert "C1" in meta["validation"]["codes"]


def test_a_cause_nothing_it_read_states_is_flagged_as_its_sentence(db_path):
    rid = _rid(db_path)
    answer, meta = _finish("Labor ran 31.4%. Ratings fell because the patio closed.", rid)
    assert meta["unsupported_causes"] == ["Ratings fell because the patio closed."]
    assert "Unsupported cause" in " ".join(meta["validation"]["caveats"])


def test_another_tenants_name_is_dropped_from_the_answer(db_path):
    rid = _rid(db_path)
    _rid(db_path, "Gia Mia")
    answer, meta = _finish("Labor ran 31.4%. Unlike Gia Mia down the street, you run lean on Fridays.", rid)
    assert "Gia Mia" not in answer and answer.startswith("Labor ran 31.4%.")
    assert "T1" in meta["validation"]["codes"]


def test_a_tenant_the_restaurants_own_data_names_is_allowed(db_path):
    rid = _rid(db_path)
    _rid(db_path, "Gia Mia")
    comp = json.dumps({"is_live": True, "competitors": [{"name": "Gia Mia", "rating": 4.6}]})
    answer, _meta = _finish("Gia Mia is rated 4.6 on Google.", rid, corpus=(SNAPSHOT, comp),
                            tools_used=("read_competitors",))
    assert "Gia Mia" in answer


def test_an_action_claim_becomes_queued_for_your_ok(db_path):
    rid = _rid(db_path)
    answer, meta = _finish("I've sent the order to Fresh Co.", rid)
    assert "sent" not in answer and "queued" in answer and "for your OK" in answer
    assert "A1" in meta["validation"]["codes"]


def test_firing_named_staff_is_never_advice(db_path):
    rid = _rid(db_path)
    answer, meta = _finish("Labor ran 31.4%. Fire the Friday closer.", rid)
    assert "Fire" not in answer and "A2" in meta["validation"]["codes"]


def test_an_answer_with_nothing_left_gets_the_fixed_copy(db_path):
    rid = _rid(db_path)
    _rid(db_path, "Gia Mia")
    answer, meta = _finish("Unlike Gia Mia, you run lean.", rid)
    assert answer == ask_cavnar.ASK_REFUSED_ANSWER
    assert meta["validation"]["verdict"] == "refuse"


def test_the_route_payload_carries_causes_names_and_the_verdict(db_path, monkeypatch):
    import client_api
    rid = _rid(db_path)
    r = get_restaurant(rid, db_path=db_path)
    monkeypatch.setattr(ask_cavnar, "build_context", lambda rest: SNAPSHOT)
    monkeypatch.setattr(ask_cavnar, "create_with_retry", lambda client, **kw: types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="Ratings fell because the patio closed. Ask Marco why.")],
        stop_reason="end_turn"))
    payload, status = client_api._do_ask_cavnar(rid, "why did ratings fall?", user_id=None, user=None)
    assert status == 200
    assert payload["unsupported_causes"] == ["Ratings fell because the patio closed."]
    assert payload["unsupported_names"] == ["Marco"]
    assert payload["validation"]["verdict"] in ("caveat", "withhold")
    assert set(payload["validation"]) == {"verdict", "caveats", "controls", "codes", "version"}


def test_every_verdict_is_logged(db_path):
    rid = _rid(db_path)
    _finish("You saved $1,240 a month.", rid)
    rows = models.get_conn().execute("SELECT surface, verdict, rules FROM ai_validation_log "
                                     "WHERE restaurant_id=?", (rid,)).fetchall()
    assert [r["surface"] for r in rows] == ["ask"] and "F4" in rows[0]["rules"]
