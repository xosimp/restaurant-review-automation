"""Memory re-audit fix round (9/29/26), R9 — "pooled_prior" (PLATFORM-9, -10).

The pooled acceptance prior had no per-organisation cap (one large group set
everyone's prior) and counted every shown episode alike, whatever position it
was shown at (a popularity loop across restaurants). The capped figure holds
each organisation to scoring.MAX_RESTAURANT_SHARE and counts only episodes
shown in the first POOLED_ACCEPT_MAX_POSITION places; the prior reads it.
"""
import sys
import uuid

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
    from intelligence import jobs
    jobs.invalidate_excluded()
    models._internal_homes_cache.clear()
    yield
    jobs.invalidate_excluded()


NAMES = ("Harbor Grill", "Lakeside Diner", "Corner Bistro", "Maple Tavern", "Riverside Kitchen", "Oak Street Cafe")


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _episode(rid, action, position=1):
    rec_id = uuid.uuid4().hex
    _x("INSERT INTO rec_instances (rec_id, restaurant_id, key, first_position) VALUES (?, ?, 'trim_day:tue', ?)",
       (rec_id, rid, position))
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, rec_id) "
       "VALUES (?, 'trim_day', ?, ?, datetime('now', '-3 days'), ?)", (rid, f"trim_day:tue#e{rec_id}", action, rec_id))


def _rids():
    return [create_restaurant(Restaurant(name=n, owner_email=f"o{i}@x{i}.test")) for i, n in enumerate(NAMES)]


def test_one_large_organisation_cannot_set_everyones_acceptance_prior():
    from intelligence import scoring
    big, *others = _rids()
    for _ in range(40):
        _episode(big, "accepted")
    for rid in others:
        for _ in range(3):
            _episode(rid, "not_for_us")
    s = scoring.kind_stats("trim_day", decay=True)
    assert s["acceptance_rate"] > 0.7, "raw: the big group's taste"
    assert s["acceptance_rate_capped"] <= 0.34 + 1e-6, "capped: one organisation is at most a third"
    assert s["acceptance_rate_decayed_capped"] <= 0.34 + 1e-6


def test_an_episode_shown_far_down_does_not_count_as_ignored():
    from intelligence import scoring
    rids = _rids()
    for rid in rids:
        _episode(rid, "accepted", position=1)
        for _ in range(4):
            _episode(rid, "ignored", position=9)      # never on screen
    s = scoring.kind_stats("trim_day")
    assert s["acceptance_rate"] == 0.2
    assert s["acceptance_rate_capped"] == 1.0


def test_the_prior_reads_the_capped_figure(monkeypatch):
    import intelligence
    import rec_learning
    rid = create_restaurant(Restaurant(name="Asker Grill", owner_email="asker@x.test"))
    fake = {"answered": 50, "acceptance_available": True, "acceptance_rate_decayed_shrunk": 0.9,
            "acceptance_rate_decayed_capped_shrunk": 0.4, "measured": 0}
    monkeypatch.setattr(intelligence, "recommendation_success", lambda *a, **k: dict(fake))
    monkeypatch.setattr(rec_learning, "prior_rungs", lambda kind, cohort, profile: [("concept", {}, "restaurants like yours")])
    acc, _suc = rec_learning.Effectiveness(rid, []).prior("trim_day")
    assert acc == pytest.approx(0.4)
