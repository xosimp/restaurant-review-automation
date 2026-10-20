"""Scale fix round 10/19/26, F4 (database): the retention cap that follows a
table's inflow (DB-03), the sub-daily claims' short window (DB-14 / INF-16),
partition-drop retention's SQLite no-op (DB-03; its Postgres half is in
test_q2_postgres), the indexes the hot fleet and per-tenant reads seek on
(DB-13/16/17, A5-F11)."""
import sqlite3

import pytest

import models
import ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _many(db_path, sql, rows):
    c = models.get_conn(db_path)
    try:
        c.executemany(sql, rows)
        c.commit()
    finally:
        c.close()


def _n(db_path, sql, args=()):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()[0]
    finally:
        c.close()


def _log(db_path, n, days_ago):
    rid = models.create_restaurant(models.Restaurant(name="Log", owner_email=f"log{days_ago}@x.com"),
                                   db_path=db_path)
    _many(db_path, "INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at) "
                   "VALUES (?, 'x', '{}', datetime('now', ?))", [(rid, f"-{int(days_ago * 24)} hours")] * n)


# ── DB-03: the cap follows the inflow ──────────────────────────────────────

def test_the_retention_cap_follows_what_ages_out_a_day(db_path, monkeypatch):
    monkeypatch.setattr(ops, "RETENTION_PASS_MAX_ROWS", 10)
    monkeypatch.setattr(ops, "RETENTION_CHUNK_ROWS", 4)
    days = ops._RETENTION_DAYS["activity_log"]
    _log(db_path, 15, days + 0.5)               # aged out in the last day: the inflow
    _log(db_path, 20, days + 100)               # a backlog from capped nights
    out = ops.prune_ledgers(db_path)
    # The fixed cap (10) would have left the table growing; 2 x 15 = 30 go.
    assert out["activity_log"] == 30 and "activity_log" in out["capped"]
    assert _n(db_path, "SELECT COUNT(*) FROM activity_log WHERE created_at < datetime('now', ?)",
              (f"-{days} days",)) == 5
    ops.prune_ledgers(db_path)
    assert _n(db_path, "SELECT COUNT(*) FROM activity_log WHERE created_at < datetime('now', ?)",
              (f"-{days} days",)) == 0


def test_a_mis_stamped_table_still_loses_no_more_than_the_fixed_cap_a_night(db_path, monkeypatch):
    # Rows stamped decades back (a wrong clock, a bad column): nothing aged out in
    # the last day, so the cap stays the fixed one.
    monkeypatch.setattr(ops, "RETENTION_PASS_MAX_ROWS", 10)
    monkeypatch.setattr(ops, "RETENTION_CHUNK_ROWS", 4)
    _log(db_path, 50, 9000)
    out = ops.prune_ledgers(db_path)
    assert out["activity_log"] == 10 and "activity_log" in out["capped"]


# ── DB-14 / INF-16: claims of a period under a day ─────────────────────────

def test_sub_daily_claims_go_after_two_days_and_daily_ones_stay(db_path):
    rows = [("live_labor:5:2026-10-01-18-2", "-3 days"), ("intraday:5:2026-10-01-18", "-3 days"),
            ("dsr_sweep:2026-10-01-18-4", "-4 days"), ("live_labor:5:2026-10-03-18-2", "-1 days"),
            ("daily_alerts:2026-10-01", "-3 days"), ("pos_retry_attempt:5:2026-10-01#1", "-3 days"),
            ("live_labor:5:2026-09-01-18-2", "-30 days")]          # past the band: the 45-day window's
    _many(db_path, "INSERT INTO job_period_claims (job_key, claimed_at) VALUES (?, datetime('now', ?))", rows)
    out = ops.prune_ledgers(db_path)
    assert out["subdaily_claims"] == 3
    c = models.get_conn(db_path)
    try:
        left = {r[0] for r in c.execute("SELECT job_key FROM job_period_claims").fetchall()}
    finally:
        c.close()
    assert {"live_labor:5:2026-10-03-18-2", "daily_alerts:2026-10-01", "pos_retry_attempt:5:2026-10-01#1",
            "live_labor:5:2026-09-01-18-2"} <= left
    assert not left & {"live_labor:5:2026-10-01-18-2", "intraday:5:2026-10-01-18", "dsr_sweep:2026-10-01-18-4"}


def test_the_sub_daily_window_has_a_floor(db_path, monkeypatch):
    monkeypatch.setattr(ops, "SUBDAILY_CLAIMS_KEEP_DAYS", 0)
    assert ops.retention_state("subdaily_claims")["state"] == "disabled"
    st = ops._special_retention()["subdaily_claims"]
    assert st[1] == 1


def test_the_sub_daily_jobs_are_the_loops_sub_daily_claims():
    import inspect
    import labor_live
    import scheduler
    import strategy_jobs
    src = inspect.getsource(scheduler)
    for job in ("ai_batch_collect", "dsr_delivery", "staff_reminders", "pos_schedule_push",
                "live_labor", "intraday"):
        assert f'claim_period("{job}", f"{{today}}-{{now.hour}}-' in src, job
    # The DSR sweep claims its 10-minute slot in _submit_dsr_sweep (its own lane since F3, 10/19/26).
    assert 'slot = f"{now.date()}-{now.hour}-{now.minute // 10}"' in src and 'claim_period("dsr_sweep", slot)' in src
    assert 'claim_period(f"live_labor:{r.id}", f"{local.date().isoformat()}-{local.hour}-"' in \
        inspect.getsource(labor_live)
    assert 'claim_period(f"intraday:{r.id}", f"{local.date().isoformat()}-{local.hour}")' in \
        inspect.getsource(strategy_jobs)


# ── DB-03: whole-month drops are Postgres only ─────────────────────────────

def test_partition_drops_are_a_no_op_on_sqlite(db_path):
    c = models.get_conn(db_path)
    try:
        assert ops._drop_expired_months(c, "pos_ticket_lines", 400) == []
        assert ops._drop_expired_months(c, "activity_log", 1) == []
    finally:
        c.close()


# ── DB-13 / 16 / 17, A5-F11: the reads seek ─────────────────────────────────

@pytest.mark.parametrize("sql,args,index", [
    ("SELECT DISTINCT restaurant_id FROM schedule_history WHERE generated_at >= datetime('now', ?)",
     # SQLite reads it from either index alone (DISTINCT in key order); Postgres seeks the range.
     ("-56 days",), ("idx_schedule_history_fleet_gen", "COVERING INDEX idx_schedule_history_generated")),
    ("SELECT id FROM schedule_history WHERE restaurant_id=? AND week_start=?", (1, "2026-10-12"),
     "idx_schedule_history_week"),
    ("SELECT id FROM schedule_history WHERE restaurant_id=? ORDER BY generated_at DESC LIMIT 1", (1,),
     "idx_schedule_history_generated"),
    ("SELECT * FROM restaurants WHERE organization_id=? ORDER BY location_name", (1,), "idx_restaurants_org"),
    ("SELECT * FROM restaurants WHERE location_group=? AND LOWER(TRIM(COALESCE(owner_email,'')))=?",
     ("Syrup", "a@b.com"), "idx_restaurants_group"),
    ("SELECT DISTINCT restaurant_id FROM activity_log WHERE event_type='tab_view' AND created_at >= ?",
     ("2026-10-01",), "idx_activity_log_type_created"),
    ("SELECT id FROM schedule_history WHERE hours_hourly IS NULL ORDER BY id DESC", (),
     "idx_schedule_history_no_hours"),
    ("SELECT id FROM ai_usage WHERE vendor IS NULL", (), "idx_ai_usage_no_vendor"),
])
def test_the_hot_reads_seek_an_index(db_path, sql, args, index):
    if db_path in getattr(models, "_PG_ROUTES", {}):
        pytest.skip("SQLite's planner; Postgres plans its own")
    c = sqlite3.connect(db_path)
    try:
        # A fleet's worth of old drafts and the planner's statistics, as production has.
        c.executemany("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, hours_hourly) "
                      "VALUES (?, datetime('now', ?), '2026-01-05', 100)",
                      [(i % 40, f"-{200 + i} days") for i in range(400)])
        c.execute("ANALYZE")
        plan = " ".join(str(r[-1]) for r in c.execute("EXPLAIN QUERY PLAN " + sql, args))
    finally:
        c.close()
    assert any(i in plan for i in ((index,) if isinstance(index, str) else index)), plan


# ── DB-11 / DB-02: the lines by item and day ───────────────────────────────

class _FakePOS:
    @staticmethod
    def archive_rows(rid, day):
        iso = day.isoformat()

        def line(lid, ticket, item, name, kind="sale", qty=1, sales=6.0, parent=None, cost=None, aqu=None):
            return {"line_id": f"{iso}-{lid}", "ticket_id": ticket, "business_date": iso, "item_id": item,
                    "item_name": name, "kind": kind, "qty": qty, "sales": sales,
                    "parent_line_id": f"{iso}-{parent}" if parent else None, "cost": cost, "aqu_count": aqu,
                    "department": "Bar", "sales_category": "Liquor", "price": sales, "regular_price": sales,
                    "voided": 0}

        def ticket(tid, net):
            t = {c: 0 for c in ("is_bar", "guest_count", "entree_count", "bev_count", "discount", "tip", "grat",
                                "tax", "cancelled", "type_sale", "type_discnt", "type_promo", "type_comp")}
            t.update({"ticket_id": f"{iso}-{tid}", "business_date": iso, "net_sales": net})
            return t
        return {"tickets": [ticket("T1", 30.0), ticket("T2", 6.0)],
                "lines": [line("a", "T1", "100", "Tito's", cost=1.2), line("b", "T1", "100", "Tito's", cost=1.2),
                          line("c", "T1", "900", "Double", qty=0, sales=0.0, parent="a"),
                          line("d", "T2", "200", "Miller Lite", kind="comp", qty=-1, sales=0.0),
                          line("e", "T2", "300", "  Fish  Tacos", qty=2, sales=24.0),
                          line("f", "T2", "300", "Fish Tacos", kind="refund", qty=1, sales=-12.0)],
                "punches": [], "payments": [], "payouts": [], "max_stamp": f"{iso}T23:00:00"}


def _archive(db_path, rid, day):
    import pos_archive
    return pos_archive.archive_day(rid, day, db_path=db_path, provider=("fakepos", _FakePOS))


def _rid_(db_path):
    return models.create_restaurant(models.Restaurant(name="Roll", owner_email="roll@x.com"), db_path=db_path)


def test_archive_day_rolls_its_lines_by_item_in_the_same_transaction(db_path):
    from datetime import date
    import pos_archive
    rid = _rid_(db_path)
    _archive(db_path, rid, date(2026, 10, 8))
    c = models.get_conn(db_path)
    try:
        assert c.execute("SELECT item_rollup FROM pos_archive_days WHERE restaurant_id=?", (rid,)).fetchone()[0] == \
            pos_archive.ITEM_ROLLUP_VERSION
        assert pos_archive.rolled_window(c, rid, "2026-10-08", "2026-10-08")
        assert not pos_archive.rolled_window(c, rid, "2026-10-01", "2026-10-07")     # nothing archived there
        got = [dict(r) for r in c.execute("SELECT * FROM pos_item_daily WHERE restaurant_id=?", (rid,)).fetchall()]
    finally:
        c.close()
    rows = {(r["item_id"], r["kind"], r["is_mod"], r["mult"]): r for r in got}
    # The Double doubles the one line it modifies: that line is its own row, the plain one another.
    assert rows[("100", "sale", 0, 2.0)]["lines"] == 1 and rows[("100", "sale", 0, 1.0)]["lines"] == 1
    assert sum(r["cost_lines"] for r in got if r["item_id"] == "100") == 2
    rows = {(r["item_id"], r["kind"], r["is_mod"]): r for r in got}
    dbl = rows[("900", "sale", 1)]
    assert dbl["pure_mod"] == 1 and dbl["qty_eff"] == 1.0 and dbl["kind_eff"] == "sale"
    assert rows[("200", "comp", 0)]["qty_eff"] == 1.0                    # a comp's units are counted positive
    assert rows[("300", "refund", 0)]["units"] == 1.0                     # every kind is kept


def test_recipes_read_the_same_sales_from_the_rollup_as_from_the_lines(db_path, monkeypatch):
    from datetime import date
    import backoffice_recipes
    import pos_archive
    rid = _rid_(db_path)
    _archive(db_path, rid, date(2026, 10, 8))
    _archive(db_path, rid, date(2026, 10, 9))
    args = (rid, ["Fish Tacos", "Tito's"], "2026-10-08", "2026-10-09")
    rolled = backoffice_recipes._pos_sales(*args, db_path=db_path)
    by_day = backoffice_recipes._pos_sales(*args, db_path=db_path, by_day=True)
    monkeypatch.setattr(pos_archive, "rolled_window", lambda *a, **k: False)
    assert backoffice_recipes._pos_sales(*args, db_path=db_path) == rolled
    assert backoffice_recipes._pos_sales(*args, db_path=db_path, by_day=True) == by_day
    assert rolled[backoffice_recipes._pos_key("Fish Tacos")] == (4.0, 48.0)


def test_the_nightly_roll_fills_older_days_and_keeps_the_lines_while_one_is_due(db_path):
    from datetime import date, timedelta
    import pos_archive
    rid = _rid_(db_path)
    _archive(db_path, rid, date(2026, 10, 8))
    c = models.get_conn(db_path)
    try:
        c.execute("UPDATE pos_archive_days SET item_rollup=NULL")
        c.execute("DELETE FROM pos_item_daily")
        c.commit()
    finally:
        c.close()
    assert pos_archive.roll_item_days(db_path) == {"rolled": 1, "left": 0}
    c = models.get_conn(db_path)
    try:
        assert c.execute("SELECT COUNT(*) FROM pos_item_daily WHERE restaurant_id=?", (rid,)).fetchone()[0] > 0
        old = (date.today() - timedelta(days=ops._RETENTION_DAYS["pos_ticket_lines"] + 10)).isoformat()
        c.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date) VALUES (?, 'fakepos', ?)",
                  (rid, old))
        c.commit()
    finally:
        c.close()
    # A day the line window has reached and no time left to roll it: the rollup refuses, so the prune
    # keeps the lines tonight.
    with pytest.raises(RuntimeError, match="not rolled"):
        pos_archive.roll_item_days(db_path, max_seconds=0)
    assert pos_archive.roll_item_days(db_path)["rolled"] == 1


def test_the_line_window_is_its_own_and_the_rollup_outlives_it():
    import inspect
    assert ops._RETENTION_ROLLUP["pos_ticket_lines"] == "pos_archive:roll_item_days"
    assert ops._RETENTION_DAYS["pos_item_daily"] >= ops._RETENTION_DAYS["pos_ticket_lines"]
    assert "RETAIN_POS_LINES_DAYS" in inspect.getsource(ops)


# ── DB-13: one row read to tell a restaurant's POS ─────────────────────────

def test_the_connected_provider_reads_the_restaurant_once(db_path, monkeypatch):
    import pos
    rid = models.create_restaurant(models.Restaurant(name="Pos", owner_email="pos@x.com"), db_path=db_path)
    reads = []
    real = models._restaurant_from_row
    monkeypatch.setattr(models, "_restaurant_from_row", lambda row: reads.append(1) or real(row))
    assert pos.connected_provider(rid) == (None, None)
    assert len(pos.get_providers()) >= 4 and len(reads) == 1           # was one read per provider


# ── DB-08: tenant writes ───────────────────────────────────────────────────

def test_a_tenant_write_is_begin_immediate_on_sqlite(tmp_path):
    import db_dialect
    c = sqlite3.connect(str(tmp_path / "x.db"), isolation_level=None)
    try:
        db_dialect.begin_tenant_write(c, "shift_facts", 5)
        assert c.in_transaction
        c.execute("ROLLBACK")
    finally:
        c.close()


def test_the_sync_write_lock_is_the_restaurants_own_on_postgres(monkeypatch):
    import pos
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pos._write_lock_for(1) is pos._WRITE_LOCK is pos._write_lock_for(2)       # SQLite: one writer
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    one, two = pos._write_lock_for(1), pos._write_lock_for(2)
    assert one is not two and one is pos._write_lock_for(1) and one is not pos._WRITE_LOCK


def test_the_nightly_syncs_hot_writes_are_tenant_transactions():
    import inspect
    import people
    import shift_facts
    src = inspect.getsource(shift_facts)
    assert 'conn.execute("BEGIN IMMEDIATE")' not in src and src.count("    _tenant_write(conn, ") == 5
    assert 'begin_tenant_write(conn, "people"' in inspect.getsource(people.resolve_rows)
