"""Blind re-audit 10/20/26, refix G3, on a production-shaped Postgres: the
archive's new statements on the PARTITIONED fact tables (pos_punches,
pos_ticket_lines — the suite's Postgres mode builds them unpartitioned, so
this copies a SQLite database through pg_copy as the cutover did).

  * a punch re-filed on its business date (rpower.punch_business_date) is
    moved off the neighbouring night, never kept on both (R3-10 / R9-09);
  * the shrink guard keeps a stored night against an empty re-read (R3-05);
  * the price-list check reads the partitioned lines (R3-18).

Needs CAVNAR_TEST_PG_URL (a scratch server); skipped without one."""
import contextlib
import io
import os
import sqlite3
import uuid
from datetime import date

import pytest

import db_dialect
import models

PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
live = pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")


def _url(name):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(PG)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
def scratch_db():
    import psycopg
    name = f"g3live_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _url(name)
    db_dialect.close_idle(_url(name))
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


class _Prov:
    def __init__(self, punches=(), lines=1):
        self.punches, self.lines = list(punches), lines

    def archive_rows(self, rid, day):
        iso = day.isoformat()
        return {"tickets": [{"ticket_id": "t1", "business_date": iso, "net_sales": 10.0}],
                "lines": [{"line_id": f"l{i}", "ticket_id": "t1", "business_date": iso, "kind": "sale", "qty": 1,
                           "sales": 10.0, "item_id": "M1"} for i in range(self.lines)],
                "punches": [dict(p, business_date=iso) for p in self.punches], "payments": [], "payouts": [],
                "max_stamp": "2026-10-01T03:00:00", "links_ok": True}


@live
def test_the_archives_new_statements_on_the_partitioned_tables(scratch_db, tmp_path, monkeypatch):
    import pg_copy
    import psycopg
    import ops
    import pos_archive
    db_path = str(tmp_path / "source.db")
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(db_path)
        models.ensure_columns(db_path)
    rid = models.create_restaurant(models.Restaurant(name="A", owner_email="a@x.com"), db_path=db_path)
    s = sqlite3.connect(db_path)
    # A 4:30am punch the old 5am rule filed on 10/9; and 10/9's own night.
    s.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, clock_in) "
              "VALUES (?, 'rpower', 'p430', '2026-10-09', '2026-10-10T04:30:00')", (rid,))
    s.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, clock_in) "
              "VALUES (?, 'rpower', 'p20', '2026-10-09', '2026-10-09T20:00:00')", (rid,))
    s.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_id, kind, price, "
              "regular_price, price_level_id) VALUES (?, 'rpower', 'x1', '2026-10-09', 'M1', 'sale', 9.0, 9.0, '1')",
              (rid,))
    s.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, price) "
              "VALUES (?, 'rpower', 'M1', '1', 8.0)", (rid,))
    s.commit()
    s.close()
    models.checkpoint(db_path)
    dst = psycopg.connect(scratch_db, autocommit=False)
    try:
        assert pg_copy.copy_database(db_path, dst, today=date(2026, 10, 20))["verify"]["ok"]
    finally:
        dst.close()
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: db_dialect.connect(scratch_db))
    monkeypatch.setattr(ops, "claim_cooldown", lambda *a, **k: True)
    monkeypatch.setattr(ops, "release_period", lambda *a, **k: None)
    prov = _Prov(punches=[{"punch_id": "p430", "clock_in": "2026-10-10T04:30:00"}], lines=10)
    out = pos_archive.archive_day(rid, date(2026, 10, 10), provider=("rpower", prov))
    assert out["punches"] == 1
    c = db_dialect.connect(scratch_db)
    try:
        got = sorted(tuple(r) for r in c.execute("SELECT punch_id, business_date FROM pos_punches "
                                                 "WHERE restaurant_id=?", (rid,)).fetchall())
        assert got == [("p20", "2026-10-09"), ("p430", "2026-10-10")], got
    finally:
        c.close()
    prov.lines, prov.punches = 0, []
    with pytest.raises(pos_archive.ArchiveShrunk):
        pos_archive.archive_day(rid, date(2026, 10, 10), provider=("rpower", prov))
    c = db_dialect.connect(scratch_db)
    try:
        assert c.execute("SELECT COUNT(*) FROM pos_ticket_lines WHERE restaurant_id=? AND business_date=?",
                         (rid, "2026-10-10")).fetchone()[0] == 10
        assert c.execute("SELECT suspect FROM pos_archive_days WHERE restaurant_id=? AND business_date=?",
                         (rid, "2026-10-10")).fetchone()[0]
    finally:
        c.close()
    # One item rung at a new price: not enough to read the list again.
    monkeypatch.setattr(pos_archive, "PRICE_SYNC_DAYS", 30)
    c = db_dialect.connect(scratch_db)
    try:
        c.execute("INSERT INTO pos_archive_state (restaurant_id, provider, prices_read_at) "
                  "VALUES (?, 'rpower', datetime('now'))", (rid,))
        c.commit()
    finally:
        c.close()
    assert pos_archive._prices_due(rid, "rpower", "2026-10-09", None) is False
