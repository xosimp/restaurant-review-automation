"""Scale fix round 10/19/26, F2 — the archive reads each night once: an
unchanged night a reader already stored is not read again and one (rid,
day) is archived by one caller at a time (A3-05); the change read is shared
by every process (A3-08); the change cursor advances past a failing day,
which is owed instead (A3-11); a provider that asks us to wait ends the
night (A3-10); prices are read weekly or when a sold price moved (A3-07);
the main price level is read over a bounded window (DB-10)."""
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import models
import ops
import pos
import pos_archive
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(pos_archive, "sync_catalog", lambda *a, **k: None)
    monkeypatch.setattr(pos_archive, "sync_roles", lambda *a, **k: None)
    pos_archive._change_memo.clear()
    yield


def _rid(rid=1):
    conn = models.get_conn()
    conn.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, "Arch Co", "a@x.test", "tok", 1280, "1123", "America/Chicago"))
    conn.commit()
    conn.close()
    return rid


class _Fake:
    def __init__(self, changed=(), fail=(), wait=None):
        self.changed, self.fail, self.wait, self.calls, self.change_reads = set(changed), set(fail), wait, [], 0

    def archive_rows(self, rid, day):
        self.calls.append(day.isoformat())
        if self.wait:
            time.sleep(self.wait)
        if day.isoformat() in self.fail:
            raise RuntimeError("POS hiccup")
        iso = day.isoformat()
        return {"tickets": [{"ticket_id": f"t-{iso}", "business_date": iso, "net_sales": 10.0}],
                "lines": [{"line_id": f"l-{iso}", "ticket_id": f"t-{iso}", "business_date": iso, "kind": "sale",
                           "qty": 1, "sales": 10.0, "price": 10.0, "item_id": "M1", "price_level_id": "1"}],
                "punches": [], "max_stamp": "2026-10-01T03:00:00"}

    def changed_business_dates(self, rid, since, until):
        self.change_reads += 1
        return set(self.changed)


def _now():
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).replace(microsecond=0)


TODAY = date.today()
YDAY = TODAY - timedelta(days=1)


# ── A3-05 ───────────────────────────────────────────────────────────────────

def test_a_night_a_reader_already_stored_is_not_read_again(monkeypatch):
    rid = _rid()
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.archive_day(rid, YDAY)                    # the DSR's on-demand read after the close
    fake.calls.clear()
    out = pos_archive.run_for(rid, today=TODAY, now_utc=_now())
    assert YDAY.isoformat() not in fake.calls and out["unchanged"] == [YDAY.isoformat()]


def test_a_night_the_change_read_names_is_read_again(monkeypatch):
    rid = _rid()
    fake = _Fake(changed={YDAY.isoformat()})
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.archive_day(rid, YDAY)
    fake.calls.clear()
    pos_archive.run_for(rid, today=TODAY, now_utc=_now())
    assert fake.calls[0] == YDAY.isoformat()


def test_a_failed_change_read_reads_the_night_as_before(monkeypatch):
    rid = _rid()
    fake = _Fake()
    fake.changed_business_dates = lambda *a: (_ for _ in ()).throw(RuntimeError("feed down"))
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.archive_day(rid, YDAY)
    fake.calls.clear()
    pos_archive.run_for(rid, today=TODAY, now_utc=_now())
    assert fake.calls[0] == YDAY.isoformat(), "unchecked is not unchanged"


def test_one_day_is_archived_by_one_caller_at_a_time(monkeypatch):
    rid = _rid()
    fake = _Fake(wait=0.6)
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    monkeypatch.setattr(pos_archive.time if hasattr(pos_archive, "time") else time, "sleep", time.sleep)
    got = []
    threads = [threading.Thread(target=lambda: got.append(pos_archive.archive_day(rid, YDAY))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.calls == [YDAY.isoformat()], "the second caller waited for the first read, not a second download"
    assert sorted(bool(g.get("shared")) for g in got) == [False, True]
    conn = models.get_conn()
    assert conn.execute("SELECT restated FROM pos_archive_days WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    conn.close()
    assert ops.claim_cooldown(f"pos_archive_day:{rid}:rpower:{YDAY.isoformat()}", 10), "the claim was released"


# ── A3-08 ───────────────────────────────────────────────────────────────────

def test_the_change_read_is_shared_across_processes(monkeypatch):
    rid = _rid()
    fake = _Fake(changed={"2026-10-01"})
    since = datetime.now(timezone.utc) - timedelta(hours=2)
    now = datetime.now(timezone.utc)
    out = pos_archive._changed_since(rid, fake, since, now, provider="rpower")
    pos_archive._change_memo.clear()                     # another process: no memory of its own
    again = pos_archive._changed_since(rid, fake, since + timedelta(minutes=30), now + timedelta(seconds=20),
                                       provider="rpower")
    assert out == again == {"2026-10-01"} and fake.change_reads == 1
    pos_archive._change_memo.clear()
    pos_archive._changed_since(rid, fake, since - timedelta(hours=1), now + timedelta(seconds=30), provider="rpower")
    assert fake.change_reads == 2, "a read that looked from later does not answer an earlier since"


# ── A3-11 ───────────────────────────────────────────────────────────────────

def test_the_cursor_advances_past_a_failing_restated_day_which_is_owed(monkeypatch):
    rid = _rid()
    old = YDAY - timedelta(days=5)
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.archive_day(rid, old)
    pos_archive.archive_day(rid, YDAY)
    fake.changed, fake.fail = {old.isoformat()}, {old.isoformat()}
    first = _now()
    out = pos_archive.run_for(rid, today=TODAY, now_utc=first)
    assert old.isoformat() in out["failed"]
    st = pos_archive._state(rid, "rpower", models.DB_PATH)
    assert pos_archive._utc(st["changes_checked_to"]) == first, "one failing day no longer holds the window open"
    assert json.loads(st["restate_pending"]) == [old.isoformat()]
    fake.changed, fake.fail, fake.calls = set(), set(), []
    pos_archive._change_memo.clear()
    pos_archive.run_for(rid, today=TODAY, now_utc=first + timedelta(minutes=10))
    assert old.isoformat() in fake.calls, "the owed day is retried without the feed naming it again"
    assert json.loads(pos_archive._state(rid, "rpower", models.DB_PATH)["restate_pending"]) == []


def test_a_retry_of_the_row_does_not_restore_days_the_first_try_stored(monkeypatch):
    rid = _rid()
    old = YDAY - timedelta(days=3)
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.archive_day(rid, old)
    pos_archive.archive_day(rid, YDAY)
    fake.changed, fake.calls = {old.isoformat()}, []
    time.sleep(1.1)                                        # the night's look comes after the earlier store
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pos_archive.run_for(rid, today=TODAY, now_utc=now)
    assert fake.calls.count(old.isoformat()) == 1
    fake.calls = []
    monkeypatch.setattr(pos_archive, "_state", lambda *a: {})      # the row retried before the cursor moved
    pos_archive.run_for(rid, today=TODAY, now_utc=now)
    assert old.isoformat() not in fake.calls


def test_a_provider_asking_us_to_wait_ends_the_night(monkeypatch):
    rid = _rid()
    fake = _Fake()

    def busy(r, day):
        fake.calls.append(day.isoformat())
        raise rpower.RPowerUnavailable("breaker open", retry_after=300)
    fake.archive_rows = busy
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    out = pos_archive.run_for(rid, today=TODAY, now_utc=_now())
    assert len(fake.calls) == 1 and out["retry_after"] == 300 and len(out["failed"]) > 1

    import strategy_jobs
    monkeypatch.setattr(pos_archive, "run_for", lambda r: out)
    with pytest.raises(RuntimeError) as got:
        strategy_jobs.run_queued_pos_archive(type("J", (), {"payload": {"restaurant_id": rid}})())
    assert got.value.retry_after == 300


def test_one_restaurant_can_be_sent_back_to_the_pos_alone(monkeypatch):
    rid = _rid()
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    pos_archive.archive_day(rid, YDAY - timedelta(days=9))
    day = YDAY - timedelta(days=9)
    assert pos_archive.ready_dates(rid, "rpower", [day], live=False) == {day.isoformat()}
    monkeypatch.setenv("POS_ARCHIVE_READS_OFF", f"7,{rid}")
    assert pos_archive.ready_dates(rid, "rpower", [day], live=False) == set()


# ── ONB-4 / ONB-6: a connect archives history first, then syncs ─────────────

def test_a_connect_queues_the_onboarding_and_the_demo_does_not(monkeypatch):
    import job_queue
    rid = _rid()
    store = {"cg": 1280, "store_mid": "1123", "name": "Arch Co"}
    monkeypatch.setattr(rpower, "test_token", lambda token: {"ok": True, "stores": [dict(store)]})
    assert rpower.bootstrap(rid)["ok"]
    conn = models.get_conn()
    rows = conn.execute("SELECT kind, restaurant_id FROM jobs WHERE kind='pos_onboard'").fetchall()
    conn.execute("UPDATE restaurants SET rpower_token='demo-store:halsted' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    assert [tuple(r) for r in rows] == [("pos_onboard", rid)]
    assert job_queue.HANDLERS["pos_onboard"] == "strategy_jobs:run_queued_pos_onboard"
    assert rpower.bootstrap(rid)["ok"]
    conn = models.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='pos_onboard'").fetchone()[0] == 1
    conn.close()


def test_onboarding_archives_newest_first_then_syncs(monkeypatch):
    import strategy_jobs
    rid = _rid()
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    monkeypatch.setattr(pos_archive, "_closed", lambda *a: True)
    synced = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda r, trigger="nightly": synced.append(trigger) or {"ok": True})
    out = strategy_jobs.run_queued_pos_onboard(type("J", (), {"payload": {"restaurant_id": rid}})())
    assert out["archived"] == pos_archive.ONBOARD_DAYS and synced == ["connect"]
    assert fake.calls[0] > fake.calls[-1], "newest night first"
    fake.calls.clear()
    assert pos_archive.onboard(rid)["archived"] == [] and fake.calls == [], "a night is read once"


def test_onboarding_stops_and_reschedules_when_the_pos_asks_us_to_wait(monkeypatch):
    import strategy_jobs
    rid = _rid()
    fake = _Fake()

    def busy(r, day):
        fake.calls.append(day.isoformat())
        raise rpower.RPowerUnavailable("open", retry_after=200)
    fake.archive_rows = busy
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    monkeypatch.setattr(pos_archive, "_closed", lambda *a: True)
    with pytest.raises(RuntimeError) as got:
        strategy_jobs.run_queued_pos_onboard(type("J", (), {"payload": {"restaurant_id": rid}})())
    assert got.value.retry_after == 200 and len(fake.calls) == 1


# ── A3-07 prices, DB-10 ─────────────────────────────────────────────────────

def test_prices_are_read_weekly_or_when_a_sold_price_moved(monkeypatch):
    rid = _rid()
    fake = _Fake()
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", fake))
    assert pos_archive._prices_due(rid, "rpower", YDAY.isoformat(), models.DB_PATH), "never read: due"
    pos_archive.archive_day(rid, YDAY)
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, price, seen_at) "
                 "VALUES (?, 'rpower', 'M1', '1', 10.0, datetime('now'))", (rid,))
    conn.commit()
    assert not pos_archive._prices_due(rid, "rpower", YDAY.isoformat(), models.DB_PATH)
    conn.execute("UPDATE pos_menu_prices SET price=12.0")
    conn.commit()
    assert pos_archive._prices_due(rid, "rpower", YDAY.isoformat(), models.DB_PATH), "a sold price moved"
    conn.execute("UPDATE pos_menu_prices SET price=10.0, seen_at=datetime('now', '-8 days')")
    conn.commit()
    conn.close()
    assert pos_archive._prices_due(rid, "rpower", YDAY.isoformat(), models.DB_PATH), "a week old"


def test_the_main_price_level_is_read_over_a_bounded_window():
    import inspect
    src = inspect.getsource(pos_archive.sync_prices)
    main = src[src.index("GROUP BY price_level_id") - 260:src.index("GROUP BY price_level_id")]
    assert "business_date>=?" in main
