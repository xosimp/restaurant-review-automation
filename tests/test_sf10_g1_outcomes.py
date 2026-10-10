"""Schedule fix round 10/10/26, workstream G1 — shifts read on outcomes a
lean shift does not win, one reading set for all (LEARNING-12), the owner's
pair suggestions served from the Holm-tested memory (LEARNING-13), and
recommendations judged on outcomes, not the engine's own score
(LEARNING-24)."""
import json

import pytest

import models
import schedule_intel as si
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    yield


def _r(**kw):
    out = {k: None for k in si.READING_KEYS}
    out.update(kw)
    return out


def test_one_reading_set_for_every_shift_and_a_shift_missing_one_is_not_read():
    readings = {("2026-09-04", "night"): _r(sales_resid=0.1, issues=0),
                ("2026-09-11", "night"): _r(sales_resid=-0.2, issues=0),
                ("2026-09-18", "night"): _r(sales_resid=0.05, issues=1),
                ("2026-09-25", "night"): _r(sales_resid=0.2)}          # not watched: no issue reading
    assert si.reading_set(readings) == ["sales_resid", "issues"]
    got = si.ran_well(readings)
    assert got == {("2026-09-04", "night"): True, ("2026-09-11", "night"): False,
                   ("2026-09-18", "night"): False}, "a shift with fewer readings faced fewer conditions"


def test_a_lean_shift_does_not_read_well_on_comps_and_turns():
    fri = ["2026-09-04", "2026-09-11", "2026-09-18", "2026-09-25"]
    readings = {(d, "night"): _r(comps_rate=c, turn_minutes=t) for d, c, t in
                zip(fri, (0.01, 0.012, 0.011, 0.04), (60, 62, 58, 95))}
    got = si.ran_well(readings)
    # The last Friday ran short (comps up, turns slow): it did not run well,
    # whatever its sales per labor hour would have said.
    assert got[(fri[3], "night")] is False and got[(fri[0], "night")] is True


def _rid():
    return create_restaurant(Restaurant(name="Teams Co", owner_email="t@x.test", module_labor=1))


def _pair_memory(rid, a, others, kind="prefer", status="candidate", p=0.004):
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, value_json, "
                 "status, enforcement, confidence) VALUES (?,?,?,?,?,?,?,?,?)",
                 (rid, f"pair|{kind}|{a}+{'+'.join(others)}", "pair", "team", a,
                  json.dumps({"with": others, "kind": kind, "shared": 9, "ran_well": 8, "baseline": 0.42,
                              "held_to": 0.45, "p_value": p, "size": 1 + len(others)}), status,
                  "soft" if status == "active" else "prompt", 0.7))
    conn.commit()
    conn.close()


def test_suggestions_come_from_the_holm_tested_pairs_with_their_baseline():
    rid = _rid()
    _pair_memory(rid, "Ana", ["Ben"])
    _pair_memory(rid, "Cy", ["Dee", "Eve"])                  # a trio: told to the model, never offered
    _pair_memory(rid, "Fay", ["Gus"], kind="avoid")          # keep-apart: the owner's call, not offered
    _pair_memory(rid, "Hal", ["Ivy"], status="retired")
    got = si.chemistry_suggestions(rid)
    assert [(g["a"], g["b"]) for g in got] == [("Ana", "Ben")]
    g = got[0]
    assert g["shared"] == 9 and g["clean_rate"] == round(8 / 9, 2) and g["baseline"] == 0.42 and g["p_value"] == 0.004
    assert "45%" in g["evidence"] and g["rec_key"] == si.pair_rec_key("Ana", "Ben")
    # Already the owner's pair: not offered again.
    conn = models.get_conn()
    conn.execute("INSERT INTO staff_pairs (restaurant_id, employee_a, employee_b, kind) VALUES (?,?,?,?)",
                 (rid, "Ana", "Ben", "prefer"))
    conn.commit()
    conn.close()
    assert si.chemistry_suggestions(rid) == []


def test_a_recommendation_needs_outcomes_not_the_engines_own_score(monkeypatch):
    import datetime as dt
    accepted = dt.date(2026, 6, 6)                            # a Saturday
    before = {((accepted - dt.timedelta(weeks=k)).isoformat(), "night"): False for k in range(1, 7)}
    after = {((accepted + dt.timedelta(weeks=k)).isoformat(), "night"): True for k in range(0, 8)}
    res = si._outcome_verdict({}, {**before, **after}, accepted.isoformat(), "Saturday", "night",
                              accepted + dt.timedelta(weeks=9))
    assert res["verdict"] == "improved" and res["before_rate"] == 1.0 and res["after_rate"] == 0.0
    few = si._outcome_verdict({}, {**before, **dict(list(after.items())[:3])}, accepted.isoformat(), "Saturday",
                              "night", accepted + dt.timedelta(weeks=9))
    assert few["verdict"] == "unknown"
