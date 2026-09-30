"""Memory re-audit fix round (9/29/26), R9 — "benchmark_hash" (FORGET-8) and
"fail_open" (PLATFORM-11, -14).

  * org_hash is an HMAC under this install's own key: "cavnar-org:r5" no
    longer recovers a restaurant's band values. Member lists leave bands
    older than any reader serves, and a deleted restaurant alone in its
    organisation is taken out of every stored band's member list.
  * Eligibility and provenance reads fail closed: an unreadable read raises
    (the pooled stage is skipped and captured) instead of teaching
    everything.
  * One "may teach" predicate: band membership and the nightly passes'
    restaurant list honour models.learning_exclusion (override, internal
    billing, test name), not exclude_from_learning alone.
"""
import hashlib
import json
import sqlite3
import sys
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    from intelligence import jobs, privacy
    jobs.invalidate_excluded()
    privacy._org_secret_cache.update(key=None, value=None)
    models._internal_homes_cache.clear()
    yield
    jobs.invalidate_excluded()
    privacy._org_secret_cache.update(key=None, value=None)


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute("PRAGMA foreign_keys=OFF")
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _q(sql, args=()):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


# ── FORGET-8 ────────────────────────────────────────────────────────────────

def test_org_hash_is_keyed_not_a_bare_hash():
    from intelligence import privacy
    bare = hashlib.sha256(b"cavnar-org:r5").hexdigest()[:16]
    h = privacy.org_hash("r5")
    assert h != bare and len(h) == 16 and h == privacy.org_hash("r5")
    assert _q("SELECT COUNT(*) AS n FROM app_secrets WHERE name LIKE 'intel_org_hash:%'")[0]["n"] == 1


def test_member_lists_leave_bands_past_the_serving_window():
    from intelligence import benchmarks, features
    old = features.iso_week(date.today() - timedelta(weeks=benchmarks.MAX_BAND_AGE_WEEKS + 2))
    new = features.iso_week(date.today())
    for wk in (old, new):
        _x("INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, vals_json, orgs, "
           "max_org_share, members_json) VALUES ('fsr', 'labor_pct_28d', ?, 5, 1, 2, 3, 2, '[1,2,3,4,5]', 5, 0.2, "
           "'[[1, \"a\"], [2, \"b\"]]')", (wk,))
    out = benchmarks.strip_old_members()
    assert out["stripped"] == 1
    rows = {r["week"]: r for r in _q("SELECT week, members_json, vals_json FROM intel_benchmarks")}
    assert rows[old]["members_json"] is None and rows[old]["vals_json"] == "[1,2,3,4,5]"
    assert rows[new]["members_json"] is not None


def test_a_deleted_restaurant_leaves_every_stored_band():
    from intelligence import benchmarks, privacy
    rid = create_restaurant(Restaurant(name="Gone Grill", owner_email="gone@x.test"))
    other = create_restaurant(Restaurant(name="Stays Cafe", owner_email="stays@x.test"))
    mine = benchmarks.viewer_org(rid)
    theirs = next(iter(benchmarks.viewer_org(other)))
    members = [[30.0, next(iter(mine))], [31.0, theirs]]
    _x("INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, vals_json, orgs, max_org_share, "
       "members_json) VALUES ('fsr', 'labor_pct_28d', '2026-W40', 2, 1, 2, 3, 2, '[30,31]', 2, 0.5, ?)",
       (json.dumps(members),))
    models.delete_restaurant(rid)
    left = json.loads(_q("SELECT members_json FROM intel_benchmarks")[0]["members_json"])
    assert left == [[31.0, theirs]]


# ── PLATFORM-11 ─────────────────────────────────────────────────────────────

class _Broken:
    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def close(self):
        pass


def test_the_google_read_fails_closed():
    from intelligence import provenance
    with pytest.raises(sqlite3.OperationalError):
        provenance.google_connected_ids(conn=_Broken())


def test_a_row_whose_eligibility_cannot_be_read_teaches_nothing(monkeypatch):
    from intelligence import jobs
    rid = create_restaurant(Restaurant(name="Odd Row Co", owner_email="o@x.test"))
    jobs.invalidate_excluded()

    def boom(*a, **k):
        raise ValueError("bad row")
    monkeypatch.setattr(models, "learning_exclusion", boom)
    assert rid in jobs.excluded_learning_ids()


def test_the_pooled_feature_read_raises_when_eligibility_is_unreadable(monkeypatch):
    from intelligence import features, jobs
    monkeypatch.setattr(jobs, "excluded_learning_ids",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError):
        features.latest_by_restaurant()


# ── PLATFORM-14 ─────────────────────────────────────────────────────────────

def test_band_membership_reads_the_one_may_teach_predicate():
    from intelligence import jobs
    inc = create_restaurant(Restaurant(name="Harbor Grill", owner_email="h@x.test"))
    ovr = create_restaurant(Restaurant(name="Lakeside Diner", owner_email="l@x.test"))
    internal = create_restaurant(Restaurant(name="Corner Bistro", owner_email="c@x.test"))
    _x("UPDATE restaurants SET learning_override='exclude' WHERE id=?", (ovr,))
    _x("UPDATE restaurants SET billing_status='internal' WHERE id=?", (internal,))
    jobs.invalidate_excluded()
    models._internal_homes_cache.clear()
    info = jobs.member_info()
    assert info[inc]["excluded"] is False
    assert info[ovr]["excluded"] is True and info[internal]["excluded"] is True
    seeded = jobs.seeded_restaurant_ids()
    assert {ovr, internal} <= seeded and inc not in seeded
    assert inc in jobs.real_restaurant_ids() and ovr not in jobs.real_restaurant_ids()
