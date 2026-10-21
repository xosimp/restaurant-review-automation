"""Audit #23 (10/21/26), fix group F6 — monitoring and the operator console.

The RPOWER health rollup (A6-01, A4-10): one precomputed row per store with
the last good time of each data type, coverage, gaps, the queue, the
ledger, the anomalies and the integrity checks; the fleet rules evaluated
from it (A6 section 3.6: A6-02, A6-12, A6-17, A4-16); data findings kept
apart from job failures (A6-04); the auth latch surfaced to the console and
the owner (A6-09, A6-10); the console's paged views (A6-15); the status
page and the fleet POS reading share-gated and off the rollup (A6-14,
A6-16); the DSR SLA counting provisional nights (A6-20); the storm page
share-gated (A6-26); a POS error critical only for auth or after the
retries (A6-22).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import models
import ops
import rpower_health as rh


def _store(db_path, name="Store", token="tok-1", cg=101, mid="M1", **kw):
    rid = models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name[:4].lower()}@x.test"),
                                   db_path=db_path)
    fields = {"rpower_token": token, "rpower_cg": cg, "rpower_store_mid": mid,
              "rpower_verified_at": "2026-08-01 10:00:00",
              "rpower_last_synced": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")}
    fields.update(kw)
    models.update_restaurant(rid, fields, db_path=db_path)
    return rid


def _nights(db_path, rid, dates, **cols):
    conn = models.get_conn(db_path)
    try:
        for d in dates:
            row = {"restaurant_id": rid, "provider": "rpower", "business_date": d, "tickets": 40, "lines": 120,
                   "punches": 12, "net_sales": 3000.0}
            row.update(cols.get(d, {}) if isinstance(cols.get(d), dict) else {})
            ks = ", ".join(row)
            conn.execute(f"INSERT INTO pos_archive_days ({ks}) VALUES ({', '.join('?' for _ in row)})",
                         tuple(row.values()))
        conn.commit()
    finally:
        conn.close()


def _local_days(n, back_from=2):
    """The last `n` dates ending `back_from` days before today (Chicago)."""
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    return [(today - timedelta(days=back_from + i)).isoformat() for i in range(n)][::-1]


def _row(db_path, rid):
    return rh.store_rows(db_path=db_path)[rid]


# ── the table is made at boot ───────────────────────────────────────────────

def test_the_rollup_tables_exist_at_boot(db_path):
    conn = models.get_conn(db_path)
    try:
        conn.execute("SELECT restaurant_id, health, severity_rank FROM rpower_store_health").fetchall()
        conn.execute("SELECT date, detail_json FROM rpower_fleet_health_daily").fetchall()
    finally:
        conn.close()


# ── one row per store, each data type on its own ────────────────────────────

def test_a_healthy_store_reads_ok_with_coverage_and_no_gaps(db_path):
    rid = _store(db_path)
    _nights(db_path, rid, _local_days(40, back_from=1))
    out = rh.run_rollup(db_path=db_path)
    assert out["attempted"] == 1 and out["ok"] == 1 and out["failed"] == 0 and out["hit_bound"] is False
    r = _row(db_path, rid)
    assert r["health"] == "ok", r["reasons"]
    assert r["gaps_14d"] == 0 and r["coverage_90d"] == 1.0
    assert r["type_states"]["archive"] == "ok" and r["type_states"]["labor"] == "ok"


def test_the_labor_stamp_alone_no_longer_stands_for_the_pos(db_path):
    """A6-01: labor fresh, the archive two trading nights behind with its
    rows failing — the store is failing, not 'RPOWER connected, fresh'."""
    rid = _store(db_path)
    days = _local_days(40, back_from=1)
    _nights(db_path, rid, days[:-3])
    _nights(db_path, rid, days[-2:-1], **{days[-2]: {"tickets": 0, "lines": 0, "punches": 0,
                                                     "last_error": "ArchiveCountMismatch", "attempts": 2}})
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert r["type_states"]["labor"] == "ok"
    assert r["type_states"]["archive"] == "failing" and r["health"] == "failing"
    assert any("ArchiveCountMismatch" in x for x in r["reasons"])


def test_a_missed_night_is_a_gap_and_shows_on_the_store(db_path):
    """A6-03: a night owed for over a day and missing is counted, named and
    raises the store's own console issue."""
    rid = _store(db_path)
    days = _local_days(40, back_from=1)
    missing = days[-5]
    _nights(db_path, rid, [d for d in days if d != missing])
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert r["gaps_14d"] == 1 and r["gap_dates"] == [missing] and r["health"] == "watch"
    issues = rh.store_issues(r)
    assert [i["key"] for i in issues] == ["rpower:gap"] and missing in issues[0]["detail"]


def test_anomalies_are_counted_from_the_nights_columns(db_path):
    rid = _store(db_path)
    days = _local_days(30, back_from=1)
    last = days[-3:]
    _nights(db_path, rid, days, **{last[0]: {"recon_gap": 90.0, "pay_gap": 80.0, "open_punches": 2},
                                   last[1]: {"recon_gap": -60.0, "pay_gap": 75.0, "unknown_sales": 400.0},
                                   last[2]: {"punches": 0, "first_read": json.dumps({"verdict": "suspect"})}})
    rh.run_rollup(db_path=db_path)
    r = _row(db_path, rid)
    assert (r["recon_nights_7d"], r["pay_gap_nights_7d"], r["open_punches_7d"], r["zero_labor_days_7d"],
            r["first_read_suspect_7d"]) == (2, 2, 2, 1, 1)
    assert r["unknown_sales_7d"] == 400.0 and r["has_anomaly"] == 1
    data = [i for i in rh.store_issues(r) if i["key"] == "rpower:data"]
    assert data and "lines vs ticket totals on 2 nights" in data[0]["detail"]


def test_integrity_checks_duplicate_binding_store_key_and_simulated(db_path):
    a = _store(db_path, "Alpha", cg=7, mid="S1")
    b = _store(db_path, "Beta", token="tok-2", cg=7, mid="S1")
    c = _store(db_path, "Gamma", token="demo-store:halsted-rye", cg=8, mid="S9")
    _nights(db_path, a, _local_days(20, back_from=1), **{_local_days(20, back_from=1)[0]: {"store_key": "7:OTHER"}})
    rh.run_rollup(db_path=db_path)
    rows = rh.store_rows(db_path=db_path)
    assert rows[a]["duplicate_binding"] == 1 and rows[b]["duplicate_binding"] == 1
    assert rows[a]["store_key_mismatch"] == 1 and rows[a]["health"] == "failing"
    assert rows[c]["simulated_not_demo"] == 1
    keys = {i["key"] for i in rh.store_issues(rows[c])}
    assert "rpower:integrity:simulated_not_demo" in keys


def test_a_demo_store_on_the_same_store_is_not_a_duplicate_binding(db_path):
    """Demo rid 4 mirrors EJ's real store — it must keep working."""
    a = _store(db_path, "Real", cg=7, mid="S1")
    d = _store(db_path, "Demo", token="tok-2", cg=7, mid="S1", is_demo=1)
    rh.run_rollup(db_path=db_path)
    rows = rh.store_rows(db_path=db_path)
    assert rows[a]["duplicate_binding"] == 0 and rows[d]["duplicate_binding"] == 0


def test_the_rollup_is_bounded_and_resumes_from_its_cursor(db_path, monkeypatch):
    monkeypatch.setattr(rh, "BATCH", 2)
    ids = [_store(db_path, f"S{i}", token=f"t{i}", mid=f"M{i}") for i in range(5)]
    clock = iter([0.0] + [100.0] * 50)
    monkeypatch.setattr(rh.time, "monotonic", lambda: next(clock))
    out = rh.run_rollup(db_path=db_path, max_seconds=50)
    assert out["hit_bound"] is True and out["attempted"] == 2
    assert set(rh.store_rows(db_path=db_path)) == set(ids[:2])
    monkeypatch.setattr(rh.time, "monotonic", lambda: 0.0)
    out = rh.run_rollup(db_path=db_path)
    assert out["hit_bound"] is False and out["attempted"] == 5
    assert set(rh.store_rows(db_path=db_path)) == set(ids)


def test_a_store_no_longer_bound_drops_out_on_a_full_pass(db_path):
    rid = _store(db_path)
    rh.run_rollup(db_path=db_path)
    models.update_restaurant(rid, {"rpower_token": None, "rpower_store_mid": None}, db_path=db_path)
    rh.run_rollup(db_path=db_path)
    assert rid not in rh.store_rows(db_path=db_path)
