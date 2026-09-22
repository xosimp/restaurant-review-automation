"""MOD audit, Reviews, part 4: what ingest alerts about, and the weekly digest
(appendix A1 R4 and R5).

What these tests protect: connecting Google for an established restaurant
imports its history silently instead of texting the owner about 2019; one
guest review alerts once however many Google APIs it arrived through; and
the weekly digest counts the week's reviews whether or not the AI has
analysed them yet, dated the way every other owner-facing date is.

Tests marked xfail(strict=True) assert the CORRECT behaviour for a defect the
MOD audit confirmed; each flips to a failure the day it is fixed.
"""
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import gmb
import models
import notify
from models import Review, save_reviews


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    yield


def _restaurant(db_path, rid=1, **kw):
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "timezone": "America/Chicago",
            "alert_1star": 1, "urgent_via_email": 1}
    cols.update(kw)
    conn = sqlite3.connect(db_path)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


@pytest.fixture
def delivered(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "deliver_alert",
                        lambda rid, kind, sms, subj, html, review_id=None, **k: sent.append((kind, review_id)))
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    return sent


def _today_local():
    return datetime.now(ZoneInfo("America/Chicago")).strftime("%Y-%m-%dT%H:%M:%S")


def _gbp(rid, i, written, rating=1):
    name = f"accounts/1/locations/2/reviews/{i}"
    return Review(restaurant_id=rid, platform="google", external_id=name, author="Old Guest",
                  rating=rating, text="Terrible visit", review_date=written, review_name=name)


# ── MOD-REV-6: a history import is silent (R4 #2) ──────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-REV-6: fire_review_alerts has no review_date age filter, so a "
                                        "first GBP import alerts on every years-old 1-star")
def test_a_backlog_of_old_one_star_reviews_sends_no_alerts(db_path, delivered):
    rid = _restaurant(db_path)
    _n, new = save_reviews([_gbp(rid, i, f"2019-05-0{1 + i}T12:00:00Z") for i in range(5)], db_path=db_path)
    notify.fire_review_alerts(rid, "R1", new, db_path=db_path)
    assert delivered == [], f"{len(delivered)} 'Respond now' alerts about 2019"


def test_a_same_day_one_star_in_an_import_batch_still_alerts(db_path, delivered):
    """The fix must keep the one review that is genuinely new."""
    rid = _restaurant(db_path)
    batch = [_gbp(rid, i, f"2019-05-0{1 + i}T12:00:00Z") for i in range(3)] + [_gbp(rid, 99, _today_local())]
    _n, new = save_reviews(batch, db_path=db_path)
    notify.fire_review_alerts(rid, "R1", new, db_path=db_path)
    today_id = next(r.id for r in new if r.external_id.endswith("/99"))
    assert ("1star", today_id) in delivered


def _first_connect(db_path, monkeypatch, reviews):
    """One run_daily_fetch in which GBP returns `reviews` for the first time."""
    import analyser
    import drafter
    import scheduler
    import webhooks
    hooks = []
    monkeypatch.setattr(webhooks, "fire_webhook", lambda rid, event, payload: hooks.append((event, payload)))
    monkeypatch.setattr(gmb, "get_valid_token", lambda rid: "tok")
    monkeypatch.setattr(gmb, "fetch_reviews_via_gmb", lambda tok, loc, rid: list(reviews))
    monkeypatch.setattr(gmb, "fetch_gmb_logo_url", lambda *a, **k: None)
    monkeypatch.setattr(analyser, "analyse_review", lambda *a, **k: None)
    monkeypatch.setattr(drafter, "draft_response", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "auto_approve_five_stars", lambda *a, **k: 0)
    scheduler.run_daily_fetch()
    return hooks


@pytest.mark.xfail(strict=True, reason="MOD-REV-6: run_daily_fetch fires a review.received webhook for every "
                                        "row a first import inserts, however old")
def test_a_first_import_fires_no_review_received_webhooks_for_old_reviews(db_path, monkeypatch, delivered):
    rid = _restaurant(db_path, gmb_refresh_token="rt", gmb_location_id="locations/2", reviews_live=1)
    hooks = _first_connect(db_path, monkeypatch, [_gbp(rid, i, "2019-05-01T12:00:00Z") for i in range(4)])
    assert [e for e, _p in hooks if e == "review.received"] == []
    assert delivered == []


def test_a_first_import_still_announces_a_review_written_today(db_path, monkeypatch, delivered):
    rid = _restaurant(db_path, gmb_refresh_token="rt", gmb_location_id="locations/2", reviews_live=1)
    hooks = _first_connect(db_path, monkeypatch, [_gbp(rid, 7, _today_local())])
    assert len([e for e, _p in hooks if e == "review.received"]) == 1
    assert [k for k, _r in delivered] == ["1star"]


# ── MOD-REV-3 at the alert layer: one review, one alert (R4 #6) ─────────────

@pytest.mark.xfail(strict=True, reason="MOD-REV-3: the Places copy and the GBP copy of one review are two "
                                        "rows, so the owner is alerted twice about the same 1-star")
def test_one_review_arriving_through_places_and_gbp_alerts_once(db_path, delivered):
    rid = _restaurant(db_path, timezone="UTC")
    places = Review(restaurant_id=rid, platform="google",
                    external_id="google_1757000000_https://www.google.com/maps/contrib/111/reviews",
                    author="Ann", rating=1, text="Cold food, never again", review_date="2025-09-04T15:33:20")
    via_gbp = Review(restaurant_id=rid, platform="google", external_id="accounts/1/locations/2/reviews/abc",
                     author="Ann", rating=1, text="Cold food, never again", review_date="2025-09-04T15:33:20Z",
                     review_name="accounts/1/locations/2/reviews/abc")
    for copy in (places, via_gbp):
        _n, new = save_reviews([copy], db_path=db_path)
        notify.fire_review_alerts(rid, "R1", new, db_path=db_path)
    assert len(delivered) == 1, f"one guest review produced {len(delivered)} alerts"


# ── MOD-REV-17: the weekly digest (R5 #3, #4) ──────────────────────────────

def _this_week(db_path, rid, ext, rating, processed):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
        "fetched_at, processed, sentiment) VALUES (?, 'google', ?, 'G', ?, 't', datetime('now','-2 days'), "
        "datetime('now','-2 days'), ?, ?)",
        (rid, ext, rating, processed, "positive" if processed else None))
    conn.commit()
    conn.close()


@pytest.mark.xfail(strict=True, reason="MOD-REV-17: get_reviews_since requires processed=1, so the digest's "
                                        "count and average omit every review the AI has not analysed")
def test_the_digest_counts_a_review_the_ai_has_not_analysed(db_path):
    from reporter import build_report_from_db
    rid = _restaurant(db_path)
    _this_week(db_path, rid, "done", 5, processed=1)
    _this_week(db_path, rid, "stuck", 1, processed=0)
    report = build_report_from_db(rid, "R1", days=7, db_path=db_path)
    assert report.total_reviews == 2
    assert report.avg_rating == 3.0


@pytest.mark.xfail(strict=True, reason="MOD-REV-17: the digest period is strftime('%b %d') / ('%b %d, %Y'), "
                                        "not M/D/YY")
def test_the_digest_period_reads_m_d_yy(db_path):
    from reporter import build_report_from_db
    rid = _restaurant(db_path)
    report = build_report_from_db(rid, "R1", days=7, db_path=db_path)
    mdy = re.compile(r"^\d{1,2}/\d{1,2}/\d{2}$")
    assert mdy.match(report.period_start), report.period_start
    assert mdy.match(report.period_end), report.period_end


# ── R5 #5: a week with nothing to say sends nothing ────────────────────────

def _digest_run(db_path, monkeypatch, rid):
    import scheduler

    class _Ok:
        ok, error = True, None
    sent = []
    monkeypatch.setattr(models, "get_restaurants_for_digest", lambda day, *a, **k: [{"id": rid}])
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "get_owner_emails", lambda r: ["o1@x.test"])
    monkeypatch.setattr(scheduler._emails, "deliver", lambda **k: sent.append(k) or _Ok())
    scheduler.run_weekly_digests()
    return sent


def test_a_reviews_only_restaurant_with_no_reviews_this_week_gets_no_digest(db_path, monkeypatch):
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    assert _digest_run(db_path, monkeypatch, rid) == []


def test_a_reviews_only_restaurant_with_a_review_this_week_gets_its_digest(db_path, monkeypatch):
    """Control for the skip above: the digest itself still goes out."""
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    _this_week(db_path, rid, "w1", 5, processed=1)
    sent = _digest_run(db_path, monkeypatch, rid)
    assert len(sent) == 1 and sent[0]["email_type"] == "digest"
