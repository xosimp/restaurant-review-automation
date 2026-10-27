"""Release-safety fix round 10/26/26, WS2 — rollback safety (A05-05, A05-10,
A05-13; R9).

Format markers compare with == (a row a newer build wrote is never served as
this build's format, nor stamped down to it); the quarterly release drills are
recorded and shown; the runbook says what a rollback is now that the database
is Postgres."""
import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import platform_monitor as pm
import schedule_shifts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 10, 26, 18, 0, 0)


def _week(fmt):
    m = {"schedule_csv": "Name,Date\nAnn,2026-10-26\n", "published_csv": None, "published_at": None,
         "exact": 1, "fmt": fmt}
    m["source_sig"] = schedule_shifts.source_sig(m)
    return m


def test_a_projection_of_another_format_is_neither_served_nor_left():
    assert schedule_shifts._servable(_week(schedule_shifts.FORMAT)) is True
    assert schedule_shifts._needs(_week(schedule_shifts.FORMAT)) is False
    newer = _week(schedule_shifts.FORMAT + 1)          # a newer build wrote it, then a rollback
    assert schedule_shifts._servable(newer) is False
    assert schedule_shifts._needs(newer) is True
    older = _week(schedule_shifts.FORMAT - 1)
    assert schedule_shifts._servable(older) is False and schedule_shifts._needs(older) is True


def test_an_archive_day_a_newer_build_stored_is_never_stamped_down(db_path, monkeypatch):
    import models
    import pos_archive
    rid = models.create_restaurant(models.Restaurant(name="P", owner_email="p@x.test"), db_path=db_path)
    newer = pos_archive.ARCHIVE_VERSION + 1
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, version) VALUES (?,?,?,?)",
                 (rid, "rpower", "2026-10-20", newer))
    conn.commit()
    conn.close()
    monkeypatch.setattr(pos_archive, "LAYOUT_UPGRADES", {1: lambda *a: True})
    assert pos_archive._upgrade_layouts(rid, "rpower", ["2026-10-20"], db_path=db_path) == set()
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT version FROM pos_archive_days WHERE restaurant_id=?", (rid,)).fetchone()[0] == newer
    conn.close()


def test_dna_a_newer_build_wrote_is_not_read(db_path, monkeypatch):
    import json
    import models
    from intelligence import dna, predict
    rid = models.create_restaurant(models.Restaurant(name="D", owner_email="d@x.test"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO intel_dna (restaurant_id, week, dims_json, version) VALUES (?,?,?,?)",
                 (rid, "2026-W43", json.dumps({"x": 1}), dna.DNA_VERSION + 1))
    conn.commit()
    conn.close()
    assert predict._my_dims(rid, {}, db_path) is None
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE intel_dna SET version=? WHERE restaurant_id=?", (dna.DNA_VERSION, rid))
    conn.commit()
    conn.close()
    assert predict._my_dims(rid, {}, db_path) == {"x": 1}


# ── the quarterly drills (A05-05 / A05-10) ───────────────────────────────────

def test_drills_are_recorded_and_go_stale_after_100_days(db_path):
    st = pm.drill_status(db_path, now=NOW)
    assert st["rollback"]["state"] == "never" and st["pg_restore"]["state"] == "never"
    pm.record_drill("rollback", minutes=7, note="quiet Tuesday", db_path=db_path,
                    done_at=(NOW - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"))
    pm.record_drill("pg_restore", minutes=40, db_path=db_path,
                    done_at=(NOW - timedelta(days=101)).strftime("%Y-%m-%d %H:%M:%S"))
    st = pm.drill_status(db_path, now=NOW)
    assert st["rollback"]["state"] == "ok" and st["rollback"]["minutes"] == 7
    assert st["pg_restore"]["state"] == "stale" and st["pg_restore"]["age_days"] == 101
    pm.record_drill("rollback", ok=False, db_path=db_path, done_at=NOW.strftime("%Y-%m-%d %H:%M:%S"))
    assert pm.drill_status(db_path, now=NOW)["rollback"]["state"] == "stale", "a failed drill is not a pass"
    with pytest.raises(ValueError):
        pm.record_drill("chaos", db_path=db_path)


def test_the_system_card_carries_the_drills(db_path, monkeypatch):
    import models
    monkeypatch.setattr(models, "DB_PATH", db_path)
    report = pm.system_report(db_path)
    assert set(report["release_drills"]) >= {"rollback", "pg_restore"}


def test_the_record_script(db_path, monkeypatch):
    import models
    from scripts import record_drill
    monkeypatch.setattr(models, "DB_PATH", db_path)
    assert record_drill.main(["pg_restore", "--minutes", "33", "--note", "counts matched"]) == 0
    assert pm.drill_status(db_path)["pg_restore"]["minutes"] == 33


# ── the runbook (A05-05, A05-13) ─────────────────────────────────────────────

def _doc(rel):
    return " ".join(open(os.path.join(ROOT, rel), encoding="utf-8").read().split())


def test_the_runbook_no_longer_says_the_database_is_on_the_volume():
    rec = _doc("docs/ops/RECOVERY.md")
    assert "The database is on a persistent volume and is not affected by a rollback" not in rec
    for phrase in ("## Rolling back a release", "Emergency deploys come from a pushed commit, never from `railway up`",
                   "**Postgres restore drill**", "**Rollback drill**", "scripts/record_drill.py",
                   "CAVNAR_ENV=staging", "promote workflow"):
        assert phrase in rec, phrase
    manifest = _doc("ARCHITECTURE_MANIFEST.md")
    assert "## 8. Release compatibility" in manifest and "Format markers compare with `==`, never `>=`." in manifest
