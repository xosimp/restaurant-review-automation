"""Scale fix round 10/19/26, F2 — the RPOWER transport's controls: the
request ledger (A3-19), the breaker with a half-open probe, full-jitter
backoff and Retry-After by rescheduling (A3-10 / INF-11), the two-level
budget (A3-14 / ONB-3 / INF-12) and the (connect, read) timeout (A3-15)."""
import time

import pytest

import job_queue
import models
import pos
import pos_io
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)             # the fleet ceiling stands alone (R3-08)
    monkeypatch.setattr(rpower.time, "sleep", lambda s: None)
    pos_io.reset_for_tests()
    yield
    pos_io.reset_for_tests()


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test",
            "rpower_token": "tok", "rpower_cg": 1280, "rpower_store_mid": f"store-{rid}"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.ok, self.text = status, 200 <= status < 300, "x"
        self.headers = headers or {}
        self._body = body if body is not None else []

    def json(self):
        return self._body


def _ledger():
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM pos_request_ledger ORDER BY endpoint").fetchall()]
    finally:
        conn.close()


# ── A3-19: the request ledger ───────────────────────────────────────────────

def test_every_request_is_counted_by_endpoint_and_store(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [{"mid": "1"}, {"mid": "2"}]))
    rpower.sales_types(rid)
    rpower.void_reasons(rid)
    pos_io.flush()
    rows = {r["endpoint"]: r for r in _ledger()}
    assert set(rows) == {"salestype/getbycg", "voidreason/getbycg"}
    st = rows["salestype/getbycg"]
    assert st["restaurant_id"] == rid and st["n"] == 1 and st["ok"] == 1 and st["rows_out"] == 2
    assert st["b250"] + st["b1000"] + st["b3000"] + st["b10000"] + st["bslow"] == 1


def test_failures_are_counted_by_kind(monkeypatch):
    rid = _restaurant()
    answers = iter([_Resp(503), _Resp(503), _Resp(200, [])])
    monkeypatch.setattr("requests.get", lambda *a, **k: next(answers))
    rpower.sales_types(rid)
    pos_io.flush()
    row = _ledger()[0]
    assert (row["n"], row["s5xx"], row["ok"]) == (3, 2, 1)


def test_the_demo_store_never_reaches_the_ledger_or_the_breaker(monkeypatch):
    called = []
    monkeypatch.setattr("requests.get", lambda *a, **k: called.append(1))
    monkeypatch.setattr(pos_io, "breaker_allow", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gated")))
    rpower._request("demo-store:halsted", "salestype/getbycg", {"cg": 1})
    pos_io.flush()
    assert called == [] and _ledger() == []


def test_the_ledger_writes_are_batched_not_per_request(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, []))
    writes = []
    real = pos_io.flush
    monkeypatch.setattr(pos_io, "flush", lambda *a, **k: (writes.append(1), real(*a, **k))[1])
    for _ in range(20):
        rpower._request("tok", "store/get")
    assert len(writes) <= 1, "at most one write per LEDGER_FLUSH_SECONDS, not one per request"
    real()
    assert _ledger()[0]["n"] == 20


def test_the_console_summary_reads_the_ledger(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [{"mid": "1"}]))
    rpower.sales_types(rid)
    out = pos_io.summary(24)
    assert out["total"]["n"] == 1 and out["by_store"][0]["restaurant"] == "R1"
    assert out["by_endpoint"][0]["endpoint"] == "salestype/getbycg" and out["breaker"]["state"] == "closed"


def test_the_ledger_is_on_the_retention_registry():
    import ops
    assert ops._RETENTION_DAYS["pos_request_ledger"] >= 30
    assert ops._RETENTION_COLUMN["pos_request_ledger"] == "hour"


# ── A3-10 / INF-11: the breaker, jitter, Retry-After ────────────────────────

def test_the_breaker_opens_after_failures_from_two_stores_and_refuses_at_once(monkeypatch):
    sent = []

    def down(*a, **k):
        sent.append(1)
        raise ConnectionError("down")
    monkeypatch.setattr("requests.get", down)
    for rid in (1, 2, 3):
        rpower._note_store(rid)
        with pytest.raises(rpower.RPowerError):
            rpower._request("tok", "ticket/getbybusinessdate", {})
    assert pos_io.breaker_state("rpower")["state"] == "open"
    n = len(sent)
    rpower._note_store(4)
    with pytest.raises(rpower.RPowerUnavailable) as got:
        rpower._request("tok", "ticket/getbybusinessdate", {})
    assert len(sent) == n, "nothing is sent while the breaker is open"
    assert got.value.retry_after > 0


def test_one_store_alone_needs_a_longer_run_to_open_it(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(500))
    rpower._note_store(1)
    with pytest.raises(rpower.RPowerError):
        rpower._request("tok", "x", {})                 # 3 failures
    assert pos_io.breaker_state("rpower")["state"] == "closed"
    for _ in range(3):
        with pytest.raises(rpower.RPowerError):
            rpower._request("tok", "x", {})
    assert pos_io.breaker_state("rpower")["state"] == "open"


def test_a_lapsed_breaker_lets_one_probe_through_and_its_answer_closes_it(monkeypatch):
    pos_io._write_breaker("rpower", open_until=time.time() - 1, opened_at=time.time() - 200, cooldown=120.0)
    assert pos_io.breaker_allow("rpower")[0] == "probe"
    assert pos_io.breaker_allow("rpower")[0] == "open", "a second caller waits for the probe"
    pos_io.breaker_success("rpower", probe=True)
    assert pos_io.breaker_state("rpower")["state"] == "closed"


def test_a_failed_probe_reopens_with_a_longer_cooldown(monkeypatch):
    pos_io._write_breaker("rpower", open_until=time.time() - 1, opened_at=time.time() - 200, cooldown=120.0)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(503))
    with pytest.raises(rpower.RPowerUnavailable):
        rpower._request("tok", "store/get")
    st = pos_io.breaker_state("rpower")
    assert st["state"] == "open" and st["cooldown"] == 240.0


def test_backoff_is_full_jitter_and_never_sleeps_after_the_last_try(monkeypatch):
    slept = []
    monkeypatch.setattr(rpower.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(503))
    with pytest.raises(rpower.RPowerError):
        rpower._request("tok", "store/get")
    assert len(slept) == rpower.MAX_RETRIES - 1
    assert all(0 <= s <= rpower.RETRY_BACKOFF ** i for i, s in enumerate(slept))


def test_a_long_retry_after_reschedules_the_queue_row(db_path, monkeypatch):
    qid, _ = job_queue.enqueue("pos_archive", {"restaurant_id": 1}, restaurant_id=1, wake=False)
    conn = models.get_conn()
    conn.execute("UPDATE jobs SET state='running', lease_owner='w', attempts=1 WHERE id=?", (qid,))
    conn.commit()
    conn.close()
    before = time.time()
    job_queue.fail(qid, "w", rpower.RPowerRateLimited("429", retry_after=900))
    conn = models.get_conn()
    row = conn.execute("SELECT state, not_before FROM jobs WHERE id=?", (qid,)).fetchone()
    conn.close()
    assert row["state"] == "queued" and row["not_before"] >= before + 899


def test_the_timeout_is_connect_then_read():
    assert isinstance(rpower.TIMEOUT_SECONDS, tuple) and rpower.TIMEOUT_SECONDS[0] <= 5


# ── OPS-6: one fleet reading, one probe ─────────────────────────────────────

def test_a_fleet_outage_needs_two_stores_and_a_share():
    import pos_health
    one = pos_health._fleet([("rpower", True, {"id": 1})])
    assert not one["rpower"]["outage"], "one store's trouble stays its own issue"
    many = pos_health._fleet([("rpower", i < 3, {"id": i}) for i in range(20)])
    assert many["rpower"]["outage"] and len(many["rpower"]["failing"]) == 3
    few = pos_health._fleet([("rpower", i < 2, {"id": i}) for i in range(100)])
    assert not few["rpower"]["outage"], "2% of the fleet is not an outage"


def test_a_fleet_outage_folds_the_stores_own_console_rows(monkeypatch):
    import admin_ops
    recs = [{"id": i, "name": f"S{i}", "issues": [{"key": "int:rpower", "severity": "critical", "since": None}]}
            for i in range(3)]
    monkeypatch.setattr(admin_ops, "_platform_issues", lambda recs, d: (
        [{"key": "fleet:pos:rpower", "severity": "critical", "since": None}], {"pos_fleet": {"rpower": {}}}))
    monkeypatch.setattr(admin_ops, "_sort_issues", lambda xs: xs)
    issues, _facts = admin_ops._all_issues(recs, {})
    assert [i["key"] for i in issues] == ["fleet:pos:rpower"]


def test_the_platform_check_names_a_fleet_pos_outage(monkeypatch):
    import ops
    import pos_health
    monkeypatch.setattr(pos_health, "fleet_state", lambda: pos_health._fleet(
        [("rpower", i < 4, {"id": i, "name": f"S{i}"}) for i in range(10)]))
    monkeypatch.setattr(pos_io, "page_if_long_open", lambda p: False)
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    out = ops.check_platform_sla(send=True)
    assert out["pos_fleet"]["rpower"]["failing"] == 4
    assert any("RPOWER sync failing for 4 of 10" in p for p in out["problems"])


def test_the_rpower_probe_reads_store_get_through_the_breaker(monkeypatch):
    import provider_health
    assert provider_health.probe_rpower()["state"] == "unconfigured"
    _restaurant(1)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [{"mid": "1", "cg": 1280}]))
    assert provider_health.probe_rpower()["state"] == "ok"
    pos_io._write_breaker("rpower", open_until=time.time() + 600, opened_at=time.time(), cooldown=600.0)
    got = provider_health.probe_rpower()
    assert got["state"] == "error" and "paused" in got["detail"]


def test_a_long_open_breaker_pages_once_per_opening(monkeypatch):
    import ops
    import scheduler
    opened = time.time() - 4000
    pos_io._write_breaker("rpower", open_until=time.time() + 60, opened_at=opened, cooldown=600.0, reason="x")
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: (pages.append(a), {"sent": True})[1])
    assert pos_io.page_if_long_open("rpower") is True
    assert pos_io.page_if_long_open("rpower") is False and len(pages) == 1


# ── A3-07: the lists, kept per consolidation group ──────────────────────────

def _stub(monkeypatch, routes):
    calls = []

    def fake(token, path, params=None):
        calls.append(path)
        return routes[path] if not callable(routes[path]) else routes[path]()
    monkeypatch.setattr(rpower, "_request", fake)
    return calls


def test_a_list_is_read_once_for_every_store_of_the_group_and_every_process(monkeypatch):
    _restaurant(1)
    _restaurant(2, rpower_store_mid="store-2")
    calls = _stub(monkeypatch, {"salestype/getbycg": [{"mid": "S", "type_sale": 1}]})
    rpower.sales_types(1)
    rpower.clear_caches()                            # another process, or a cold one after a deploy
    assert rpower.sales_types(2) == {"S": {"mid": "S", "type_sale": 1}}
    assert calls == ["salestype/getbycg"], "the second store and the second process used the kept copy"


def test_a_kept_list_older_than_its_age_is_read_again(monkeypatch):
    _restaurant(1)
    calls = _stub(monkeypatch, {"voidreason/getbycg": [{"mid": "R1", "name": "Wrong"}]})
    rpower.void_reasons(1)
    conn = models.get_conn()
    conn.execute("UPDATE pos_catalogs SET fetched_at=?", (time.time() - rpower.CATALOG_MAX_AGE_SECONDS - 5,))
    conn.commit()
    conn.close()
    rpower.clear_caches()
    rpower.void_reasons(1)
    assert calls == ["voidreason/getbycg"] * 2


def test_job_and_employee_lists_are_one_read_for_their_three_readers(monkeypatch):
    _restaurant(1)
    calls = _stub(monkeypatch, {"job/getbycg": [{"mid": "J1", "name": "Server", "ext_id": "10", "pyrlcat_mid": "P"}],
                                "employee/getbycg": [{"mid": "E1", "fname": "Ana", "lname": "B", "payroll_id": "7"}],
                                "payrollcategory/getbycg": [{"mid": "P", "name": "FOH"}]})
    rpower.fetch_people(1)
    rpower.job_categories(1)
    rpower.fetch_job_codes(1)
    rpower.pos_employees(1)
    assert sorted(calls) == ["employee/getbycg", "job/getbycg", "payrollcategory/getbycg"]


def test_a_page_load_uses_the_kept_list_at_any_age_and_never_reads_one_live(monkeypatch):
    from flask import Flask
    _restaurant(1)
    calls = _stub(monkeypatch, {"salestype/getbycg": [{"mid": "S"}], "voidreason/getbycg": [{"mid": "R"}]})
    rpower.sales_types(1)
    conn = models.get_conn()
    conn.execute("UPDATE pos_catalogs SET fetched_at=0")
    conn.commit()
    conn.close()
    rpower.clear_caches()
    calls.clear()
    with Flask("t").test_request_context("/"):
        assert "S" in rpower.sales_types(1)
        with pytest.raises(rpower.RPowerArchiveMiss):
            rpower.void_reasons(1)
        with rpower.allow_live_reads():
            assert "R" in rpower.void_reasons(1)
    assert calls == ["voidreason/getbycg"], "only the owner's explicit action read live"


def test_an_id_the_kept_list_lacks_reads_that_list_afresh_once(monkeypatch):
    from datetime import date
    _restaurant(1)
    day = "2026-10-10"
    types = [{"mid": "S", "impacts_sales": 1, "type_sale": 1}]
    routes = {"salestype/getbycg": lambda: list(types), "menuitem/getbycg": [], "salescategory/getbycg": [],
              "salesdepartment/getbycg": [], "voidreason/getbycg": [], "job/getbycg": [], "employee/getbycg": [],
              "table/getbystore": [], "room/getbystore": [], "mealtime/getbycg": [], "profitcenter/getbycg": [],
              "paymentmethod/getbycg": [], "payoutcategory/getbycg": [], "ticketitem/getbybusinessdate": [],
              "ticketpayment/getbybusinessdate": [], "payout/getbydaterange": [], "timeclock/getbydaterange": [],
              "ticket/getbybusinessdate": [],
              "ticketsales/getbybusinessdate": [{"rid": "L1", "ticket_rid": "T1", "date": f"{day}T00:00:00",
                                                 "slstype_mid": "N", "sales": 12.0, "qty": 1}]}
    calls = _stub(monkeypatch, routes)
    rpower.sales_types(1)                                   # kept before the store added type N
    types.append({"mid": "N", "impacts_sales": 1, "type_sale": 1})
    out = rpower.archive_rows(1, date(2026, 10, 10))
    assert out["lines"][0]["kind"] == "sale", "the new sales type was read, not filed unknown"
    assert calls.count("salestype/getbycg") == 2 and calls.count("menuitem/getbycg") == 1
    rpower.archive_rows(1, date(2026, 10, 10))
    assert calls.count("salestype/getbycg") == 2, "a refresh for a miss is not repeated within its window"


# ── A3-14 / ONB-3 / INF-12: the two-level budget ────────────────────────────

def test_requests_take_a_slot_in_the_token_bucket_and_the_fleet_bucket(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0.001)
    monkeypatch.setattr(rpower, "FLEET_RPS", 1000.0)
    taken = []
    real = rpower._take_shared_slot
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: (taken.append(key), real(t, s, key=key))[1])
    rpower._sleep_for_spacing("tok-a")
    assert taken[0] == rpower._bucket_key("tok-a") and taken[-1] == rpower.FLEET_BUCKET


def test_per_store_buckets_when_rpower_says_the_limit_is_per_store(monkeypatch):
    monkeypatch.setattr(rpower, "BUDGET_PER_STORE", True)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0.001)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)
    taken = []
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: (taken.append(key), 0.0)[1])
    rpower._note_store(1, "a")
    rpower._sleep_for_spacing("tok")
    rpower._note_store(2, "b")
    rpower._sleep_for_spacing("tok")
    assert taken[0] != taken[1], "one partner token, two stores, two buckets"


def test_over_budget_is_refused_not_sent_anyway(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 1.0)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)
    monkeypatch.setattr(rpower, "RATE_MAX_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda t, s, key=None: 30.0)
    sent = []
    monkeypatch.setattr("requests.get", lambda *a, **k: sent.append(1))
    with pytest.raises(rpower.RPowerBudgetExceeded) as got:
        rpower._request("tok", "store/get")
    assert sent == [] and got.value.retry_after >= 5
