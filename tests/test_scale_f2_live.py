"""Scale fix round 10/19/26, F2 — one live read per store shared by live
labor, the coverage check, the capture and /labor/live (A3-02 / A3-03), and
never a POS request on a web request thread (A3-09)."""
import time
from datetime import date

import pytest

import models
import pos
import pos_io


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


class _Prov:
    def __init__(self):
        self.sales_reads = self.clock_reads = 0
        self.sales, self.clock = 100.0, [{"employee": "Ana D", "role": "Server"}]

    def fetch_sales_today(self, rid, day):
        self.sales_reads += 1
        if self.sales is None:
            e = RuntimeError("nothing posted")
            e.not_yet = True
            raise e
        return self.sales

    def fetch_clock_ins_today(self, rid, day):
        self.clock_reads += 1
        return list(self.clock)


@pytest.fixture
def prov(monkeypatch):
    p = _Prov()
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", p))
    return p


DAY = date(2026, 10, 19)


def test_one_read_serves_every_reader_inside_the_window(prov):
    assert pos.fetch_sales_today(1, DAY) == (100.0, "rpower")
    prov.sales = 250.0
    assert pos.fetch_sales_today(1, DAY) == (100.0, "rpower"), "the kept read, not a second request"
    assert pos.fetch_clock_ins_today(1, DAY)[0][0]["employee"] == "Ana D"
    pos.fetch_clock_ins_today(1, DAY)
    assert (prov.sales_reads, prov.clock_reads) == (1, 1)


def test_an_old_read_is_read_again_and_fresh_reads_skip_the_kept_one(prov, monkeypatch):
    pos.fetch_sales_today(1, DAY)
    monkeypatch.setattr(pos_io, "LIVE_SNAPSHOT_SECONDS", 0)
    prov.sales = 250.0
    assert pos.fetch_sales_today(1, DAY)[0] == 250.0
    monkeypatch.setattr(pos_io, "LIVE_SNAPSHOT_SECONDS", 3600)
    prov.sales = 300.0
    with pos.fresh_live_reads():
        assert pos.fetch_sales_today(1, DAY)[0] == 300.0
    assert prov.sales_reads == 3


def test_nothing_posted_yet_is_shared_as_nothing_posted_yet(prov):
    prov.sales = None
    for _ in range(2):
        with pytest.raises(Exception) as got:
            pos.fetch_sales_today(1, DAY)
        assert getattr(got.value, "not_yet", False)
    assert prov.sales_reads == 1


def test_a_page_load_never_reads_the_pos(prov, monkeypatch):
    from flask import Flask
    with Flask("t").test_request_context("/"):
        with pytest.raises(pos.POSCapabilityError):
            pos.fetch_clock_ins_today(1, DAY)
    assert prov.clock_reads == 0
    pos.fetch_clock_ins_today(1, DAY)                       # the 15-minute job's read
    monkeypatch.setattr(pos_io, "LIVE_SNAPSHOT_SECONDS", 0)  # past the jobs' window, inside the page's
    with Flask("t").test_request_context("/"):
        assert pos.fetch_clock_ins_today(1, DAY)[0][0]["employee"] == "Ana D"
    assert prov.clock_reads == 1


def test_a_snapshot_of_another_day_is_not_today(prov):
    pos.fetch_sales_today(1, DAY)
    pos.fetch_sales_today(1, date(2026, 10, 20))
    assert prov.sales_reads == 2


def _rpower_restaurant():
    conn = models.get_conn()
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
                 "VALUES (1,'Ask Co','a@x.test','tok',1280,'1123','America/Chicago')")
    conn.commit()
    conn.close()
    return 1


def test_an_owners_question_off_the_request_thread_reads_the_archive_only(monkeypatch):
    import rpower
    import cogs
    import ai_utils
    _rpower_restaurant()
    monkeypatch.setattr(pos, "PROVIDERS", None)
    calls = []
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: calls.append(p) or [])
    with pos.archive_only():
        with pytest.raises(pos.POSCapabilityError):
            pos.fetch_business_days(1, date(2026, 9, 1), date(2026, 9, 7))
    assert [c for c in calls if c != "salestype/getbycg"] == [], "no live multi-day read"
    assert not cogs._for_an_owner()
    with ai_utils.ai_context(trigger="owner", actor_user_id=7):
        assert cogs._for_an_owner()


def test_asks_upcoming_orders_are_kept_for_an_hour(monkeypatch):
    import rpower
    _rpower_restaurant()
    calls = []
    monkeypatch.setattr(rpower, "_paged", lambda t, p, params, max_pages=200: calls.append(p) or [
        {"need_dttm": "2026-10-21T12:00:00", "guest_count": 20, "ticket": 5, "items": 400.0, "is_cancelled": 0}])
    a = rpower.fetch_orders_due(1, date(2026, 10, 20), date(2026, 10, 26))
    b = rpower.fetch_orders_due(1, date(2026, 10, 20), date(2026, 10, 26))
    assert a == b and a[0]["guests"] == 20 and calls == ["ticket/getbyneeddatetime"]


def test_the_coverage_check_rereads_before_it_tells_anyone(monkeypatch, db_path):
    import intraday
    import issues
    import strategy_jobs
    seen = []

    def gaps(rid, **k):
        seen.append(getattr(pos._live_ctx, "fresh", False))
        return {"available": True, "business_date": "2026-10-19", "missing": [{"employee": "Bo"}] if not seen[-1]
                else [], "arrived_keys": []}
    r = type("R", (), {"id": 1, "module_labor": 1})()
    monkeypatch.setattr(intraday, "coverage_gaps", gaps)
    monkeypatch.setattr(issues, "close_ended_coverage", lambda *a, **k: None)
    monkeypatch.setattr(issues, "get_routing", lambda *a, **k: {"manager": 1})
    monkeypatch.setattr(issues, "resolve_coverage", lambda *a, **k: None)
    monkeypatch.setattr(strategy_jobs, "_open_now", lambda *a: True)
    monkeypatch.setattr(strategy_jobs, "_raise_coverage", lambda *a, **k: pytest.fail("told on a stale read"))
    import live_activities
    monkeypatch.setattr(live_activities, "note_coverage", lambda *a, **k: None)
    out = strategy_jobs.run_coverage_check(restaurants=[r])
    assert seen == [False, True] and out["attempted"] == 1 and out["failed"] == 0
