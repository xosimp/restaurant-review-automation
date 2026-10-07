"""AI cost audit 10/7/26 — AI visibility (Perplexity).

Production, 9/10–10/7/26: 118 search-fee rows and 75 sonar rows in 27 days,
and every one of the 27 errors was "HTTP 429, no answer". About one query in
four failed after a flat 2-second retry, so most eight-query runs were
partial — and a partial run was neither stored nor cached, so the next Intel
open or Ask call started eight more live queries.

  #7   a 429 backs off (Retry-After, bounded; else exponential + jitter)
  #9   a read never runs live: stored run or "not measured yet"
  #10  a partial run is stored flagged, served, excluded from every
       comparison, and completed next time by re-asking only what failed
  #98  a read is served before the burst limit
  #46  the city is stored on the restaurant, not in a process dict
"""
import json

import pytest

import client_api
import models


HIT = "Gia Mia in Geneva is excellent."


class _Resp:
    def __init__(self, code=200, content=None, headers=None):
        self.status_code = code
        self.headers = headers or {}
        self._content = content

    def json(self):
        if self.status_code != 200 or self._content is None:
            return {}
        return {"choices": [{"message": {"content": self._content}}], "citations": ["https://x.test"]}


@pytest.fixture
def aivis(db_path, monkeypatch):
    """The real visibility path on a test database, with Perplexity faked
    per question: `fail` holds substrings of the questions that answer 429."""
    real = models.get_conn
    import notify
    for mod in (models, client_api, notify):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    conn = real(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email,google_place_id,neighborhood,"
                 "vibe,known_for) VALUES (1,'Gia Mia','o@x.test','ChIJx','Geneva',"
                 "'lively pizza bar','wood-fired pizza')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(client_api, "get_restaurant", lambda rid: models.get_restaurant(rid, db_path))
    monkeypatch.setattr(client_api, "get_review_stats", lambda rid: {"total": 10, "response_rate": 50})
    monkeypatch.setattr(client_api, "ai_budget_exceeded", lambda rid: None, raising=False)
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    city_calls = []

    def _city(pid):
        city_calls.append(pid)
        return "Geneva" if pid else ""
    monkeypatch.setattr(client_api, "_city_from_place_id", _city)
    client_api._aivis_cache.clear()
    import time as _time
    slept = []
    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))
    state = {"fail": [], "asked": [], "slept": slept, "city_calls": city_calls, "db": db_path,
             "headers": {}}

    def fake_post(url, *a, **kw):
        q = kw["json"]["messages"][-1]["content"]
        state["asked"].append(q)
        if any(f in q for f in state["fail"]):
            return _Resp(429, headers=dict(state["headers"]))
        return _Resp(200, HIT)
    import requests as _rq
    monkeypatch.setattr(_rq, "post", fake_post)
    return state


def _rows(db_path):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, ai_score, partial, failed_queries, payload_json FROM ai_visibility_runs ORDER BY id")]
    finally:
        conn.close()


def _query_rows(db_path):
    conn = models.get_conn(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM ai_visibility_query_runs").fetchone()[0]
    finally:
        conn.close()


# ── #7: a 429 backs off ────────────────────────────────────────────────────

def test_a_429_with_a_short_retry_after_is_honoured_and_resent(aivis):
    aivis["fail"] = ["tonight"]
    aivis["headers"] = {"Retry-After": "3"}
    p, _ = client_api._do_ai_visibility_inner(1, force=True)
    tonight = [q for q in aivis["asked"] if "tonight" in q]
    assert len(tonight) == client_api.AIVIS_429_MAX_SENDS, "a 429 gets more than one retry"
    assert aivis["slept"].count(3.0) == client_api.AIVIS_429_MAX_SENDS - 1
    assert p["partial"] is True


def test_a_retry_after_past_the_bound_is_not_sent_into_early(aivis):
    """Sending again before Perplexity's own window clears only collects
    another 429 — a wait longer than the bound gives the query up."""
    aivis["fail"] = ["tonight"]
    aivis["headers"] = {"Retry-After": "30"}
    client_api._do_ai_visibility_inner(1, force=True)
    assert len([q for q in aivis["asked"] if "tonight" in q]) == 1
    assert 30.0 not in aivis["slept"]
    conn = models.get_conn(aivis["db"])
    row = conn.execute("SELECT error, reason, attempts FROM ai_usage WHERE status='error' "
                       "AND action='ai_visibility'").fetchone()
    conn.close()
    assert row["reason"] == "rate_limit" and row["attempts"] == 1
    assert row["error"].startswith("HTTP 429, no answer") and "Retry-After 30" in row["error"]


def test_without_retry_after_the_backoff_is_exponential_with_jitter(monkeypatch):
    waits = []
    for sends in (1, 2, 3):
        w, why = client_api._pplx_429_wait(_Resp(429), sends, deadline=10 ** 9, now=0)
        step = min(client_api._PPLX_RETRY_AFTER_MAX, client_api._PPLX_BACKOFF_BASE * 2 ** (sends - 1))
        assert step / 2 <= w <= step, (sends, w)
        assert why.startswith("backoff")
        waits.append(step)
    assert waits == sorted(waits) and waits[0] < waits[-1]
    w, why = client_api._pplx_429_wait(_Resp(429), client_api.AIVIS_429_MAX_SENDS, deadline=10 ** 9, now=0)
    assert w is None and "still rate-limited" in why


def test_no_wait_runs_past_the_runs_deadline():
    w, why = client_api._pplx_429_wait(_Resp(429, headers={"Retry-After": "5"}), 1, deadline=3.0, now=0.0)
    assert w is None and "deadline" in why
    w, why = client_api._pplx_429_wait(_Resp(429), 3, deadline=0.5, now=0.0)
    assert w is None and "deadline" in why


def test_other_failures_keep_their_one_retry(aivis, monkeypatch):
    import requests as _rq
    asked = []

    def five_hundred(url, *a, **kw):
        asked.append(kw["json"]["messages"][-1]["content"])
        return _Resp(500)
    monkeypatch.setattr(_rq, "post", five_hundred)
    client_api._do_ai_visibility_inner(1, force=True)
    # (A run of server errors also opens the Perplexity breaker, after which
    # the rest fail fast — so some questions are sent once or not at all.)
    assert asked and max(asked.count(q) for q in set(asked)) == 2, "a server error is retried once, as before"
    assert set(aivis["slept"]) == {2.0}


def test_a_429_holds_every_sender_not_just_its_own_query(monkeypatch):
    """The limit is the key's: the gate waits past a 429's hold for every
    thread in the process."""
    import time
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(client_api, "_PPLX_MIN_INTERVAL", 0.0)
    client_api._pplx_last_sent_at[0] = 0.0
    client_api._pplx_hold_until[0] = 0.0
    try:
        client_api._pplx_hold(0.3)
        start = time.monotonic()
        client_api._pplx_wait_turn()
        assert time.monotonic() - start >= 0.25
    finally:
        client_api._pplx_hold_until[0] = 0.0


# ── #9 / #98: a read never runs live, and is never rate-limited ────────────

def test_a_read_with_nothing_on_record_says_not_measured_and_asks_nothing(aivis):
    p, status = client_api._do_ai_visibility_inner(1)
    assert status == 200 and p["ok"] is True
    assert p["state"] == "not_measured" and p["measured"] is False
    assert p["ai_score"] is None and p["ai_score_label"] == "not measured"
    assert aivis["asked"] == [], "a read ran a live Perplexity query"
    assert "checklist" in p                      # the listing is still read from records


def test_asks_read_tool_never_runs_a_live_check(aivis):
    import ask_cavnar_tools
    out = ask_cavnar_tools._read_ai_visibility(1)
    assert out["has_data"] is False and out["measured"] is False
    assert aivis["asked"] == []


def test_a_stored_read_is_served_before_the_burst_limit(aivis, monkeypatch):
    client_api._do_ai_visibility_inner(1, force=True)
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: True)
    for _ in range(4):
        client_api._aivis_cache.clear()
        p, _ = client_api._do_ai_visibility_inner(1)
        assert p["ok"] is True and p["state"] == "complete", p.get("error")
    live, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert live["ok"] is False and "Too many" in live["error"]


# ── #10: a partial run is stored, served, never compared, and completed ────

def test_a_partial_run_is_stored_flagged_and_never_compared(aivis):
    aivis["fail"] = ["tonight", "group"]
    p, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert p["partial"] is True and p["state"] == "partial"
    assert sorted(q for q in p["failed_queries"]) == sorted(
        q["query"] for q in p["queries"] if not q["ok"])
    rows = _rows(aivis["db"])
    assert len(rows) == 1 and rows[0]["partial"] == 1
    assert rows[0]["ai_score"] is None, "a partial run must never land in the trend column"
    assert len(json.loads(rows[0]["failed_queries"])) == 2
    assert _query_rows(aivis["db"]) == 0, "per-question history compares complete runs only"
    assert models.get_ai_visibility_history(1, db_path=aivis["db"]) == []
    assert models.last_two_ai_visibility_runs(1, db_path=aivis["db"]) == []
    # ...and a read serves it, flagged, without asking anything.
    aivis["asked"].clear()
    client_api._aivis_cache.clear()
    served, _ = client_api._do_ai_visibility_inner(1)
    assert served["state"] == "partial" and served["partial"] is True and served["cached"] is True
    assert aivis["asked"] == []


def test_the_next_run_this_week_re_asks_only_what_failed(aivis):
    aivis["fail"] = ["tonight", "group"]
    client_api._do_ai_visibility_inner(1, force=True)
    aivis["fail"] = []
    aivis["asked"].clear()
    p, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert len(aivis["asked"]) == 2, aivis["asked"]
    assert all(("tonight" in q or "group" in q) for q in aivis["asked"])
    assert p["partial"] is False and p["state"] == "complete" and p["reused_answers"] == 6
    rows = _rows(aivis["db"])
    assert len(rows) == 1, "the week's partial run is completed in place, not duplicated"
    assert rows[0]["partial"] == 0 and rows[0]["ai_score"] is not None
    assert rows[0]["failed_queries"] is None
    assert _query_rows(aivis["db"]) == 8
    assert len(models.get_ai_visibility_history(1, db_path=aivis["db"])) == 1


def test_a_partial_run_from_an_earlier_week_is_not_completed(aivis):
    aivis["fail"] = ["tonight"]
    client_api._do_ai_visibility_inner(1, force=True)
    conn = models.get_conn(aivis["db"])
    conn.execute("UPDATE ai_visibility_runs SET created_at=datetime('now','-8 days')")
    conn.commit()
    conn.close()
    aivis["fail"] = []
    aivis["asked"].clear()
    p, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert len(aivis["asked"]) == 8 and p["reused_answers"] == 0
    assert [r["partial"] for r in _rows(aivis["db"])] == [1, 0]


def test_an_outage_is_neither_stored_nor_served_over_the_last_run(aivis):
    client_api._do_ai_visibility_inner(1, force=True)
    aivis["fail"] = ["?", " "]                 # every question
    out, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert out["answered_queries"] == 0 and out["ai_score"] is None
    assert len(_rows(aivis["db"])) == 1
    client_api._aivis_cache.clear()
    served, _ = client_api._do_ai_visibility_inner(1)
    assert served["state"] == "complete" and served["ai_score"] is not None


def test_the_weekly_job_can_tell_a_partial_run_from_a_complete_one(aivis):
    """#38 (scheduler.py, merged separately): what the weekly pass reads to
    count a partial run as not done this week."""
    aivis["fail"] = ["tonight"]
    p, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert p["ok"] is True and p["partial"] is True and p["state"] == "partial"
    aivis["fail"] = []
    p, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert p["partial"] is False and p["state"] == "complete"


# ── #46: the city is stored on the restaurant ──────────────────────────────

def test_the_city_survives_a_deploy(aivis):
    client_api._do_ai_visibility_inner(1, force=True)
    assert aivis["city_calls"] == ["ChIJx"]
    stored = json.loads(models.get_restaurant(1, aivis["db"]).aivis_city_json)
    assert stored["place_id"] == "ChIJx" and stored["city"] == "Geneva"
    client_api._aivis_cache.clear()
    client_api._city_cache.clear()               # a deploy
    client_api._do_ai_visibility_inner(1, force=True)
    assert aivis["city_calls"] == ["ChIJx"], "the city was bought from Places again"


def test_a_corrected_place_id_reads_its_city_again(aivis):
    client_api._do_ai_visibility_inner(1, force=True)
    models.update_restaurant(1, {"google_place_id": "ChIJnew"}, db_path=aivis["db"])
    client_api._do_ai_visibility_inner(1, force=True)
    assert aivis["city_calls"] == ["ChIJx", "ChIJnew"]


def test_the_city_column_has_its_four_touch_points():
    import inspect
    import dataclasses
    assert "aivis_city_json" in {f.name for f in dataclasses.fields(models.Restaurant)}
    src = inspect.getsource(models)
    assert '("restaurants", "aivis_city_json", "TEXT")' in src
    assert '"aivis_city_json"' in inspect.getsource(models.update_restaurant)
    assert 'aivis_city_json=row["aivis_city_json"]' in src
