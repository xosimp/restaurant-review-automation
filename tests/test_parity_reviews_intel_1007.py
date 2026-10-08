"""Web vs iOS parity audit (10/7/26) — Reviews and Intel, the backend half.

#9   a review answered elsewhere carries the reply actually posted, its
     source and its date on the inbox row, always (never a missing key).
#21  `post_failed` on the row is the one rule for "Couldn't post to Google":
     approved, Google, Business Profile connected, not posted. The web card
     reads it too.
#34  the review stats carry what one bulk publish may post ("Publish N
     ready") and how many it holds back.
#52  the web's website-analytics card has Read now (/api/web-analytics/sync).
#75  a live AI-visibility check asked with `async` is a job on the owner AI
     pool, on both twins, polled through /ai-jobs.
Also: removing a competitor carries the same plan gate as adding one.
"""
import os
import sqlite3
import sys

import pytest
from flask import Flask

import ai_async
import client_api
import mobile_api
import models
from models import Restaurant, Review, create_restaurant, save_reviews

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.test"), db_path=db_path)


def _review(rid, db_path, name, platform="google", status="drafted", **cols):
    save_reviews([Review(restaurant_id=rid, platform=platform, external_id=name, author="Ann", rating=4,
                         text="Fine", review_date="2026-10-01T18:32:50", review_name=name)], db_path=db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE reviews SET response_status=?, draft_response='Thanks!', processed=1 WHERE review_name=?",
              (status, name))
    for k, v in cols.items():
        c.execute(f"UPDATE reviews SET {k}=? WHERE review_name=?", (v, name))
    c.commit()
    out = c.execute("SELECT id FROM reviews WHERE review_name=?", (name,)).fetchone()[0]
    c.close()
    return out


def _connect_gbp(db_path, rid):
    c = sqlite3.connect(db_path)
    c.execute("UPDATE restaurants SET gmb_refresh_token='tok' WHERE id=?", (rid,))
    c.commit()
    c.close()


def _rows(rid):
    return {r["id"]: r for r in models.get_reviews_data(rid)}


# ── #21 post_failed, one rule ──────────────────────────────────────────────

def test_an_approved_google_reply_is_a_failed_post_only_with_a_connection(rid, db_path):
    g = _review(rid, db_path, "g/approved", status="approved")
    y = _review(rid, db_path, "y/approved", platform="yelp", status="approved")
    p = _review(rid, db_path, "g/posted", status="posted")
    d = _review(rid, db_path, "g/drafted")
    rows = _rows(rid)
    assert not any(rows[i]["post_failed"] for i in (g, y, p, d)), "not connected: it waits to post"
    _connect_gbp(db_path, rid)
    rows = _rows(rid)
    assert rows[g]["post_failed"] is True
    assert rows[y]["post_failed"] is False and rows[p]["post_failed"] is False and rows[d]["post_failed"] is False


def test_the_web_card_reads_the_same_field():
    card = _read("templates/_review_card.html")
    assert "{% elif r.post_failed %}{% set _st = ('bad', \"Couldn't post to Google\") %}" in card
    assert "{% if r.post_failed %}" in card
    assert "gmb_refresh_token" not in card, "one rule: the row's post_failed, not a second copy"


def test_the_card_renders_the_failed_pill_and_retry_from_the_row(rid, db_path):
    from flask import render_template
    from time_utils import mdy
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.jinja_env.filters.setdefault("format_date", lambda v, *a, **k: mdy(v) if v else "")
    g = _review(rid, db_path, "g/approved", status="approved")
    _connect_gbp(db_path, rid)
    row = _rows(rid)[g]
    with app.test_request_context("/"):
        html = render_template("_review_card.html", r=row, restaurant=models.get_restaurant(rid), delay=0)
    assert "Couldn&#39;t post to Google" in html or "Couldn't post to Google" in html
    assert "retryPostR(" in html


# ── #9 the reply actually posted ─────────────────────────────────────────────

def test_the_row_carries_the_external_reply_its_source_and_date(rid, db_path):
    a = _review(rid, db_path, "g/a")
    assert models.mark_replied_elsewhere(a, rid, source="google", reply_text="Thanks Ann — Danny",
                                         replied_at="2026-10-03T12:00:00Z", db_path=db_path)
    b = _review(rid, db_path, "g/b")
    row_a, row_b = _rows(rid)[a], _rows(rid)[b]
    assert row_a["replied_elsewhere"] is True
    assert (row_a["external_reply"], row_a["external_reply_source"], row_a["external_reply_at"]) == \
        ("Thanks Ann — Danny", "google", "2026-10-03T12:00:00Z")
    for k in ("external_reply", "external_reply_source", "external_reply_at", "posted_at"):
        assert k in row_b and row_b[k] is None


# ── #34 Publish N ready ─────────────────────────────────────────────────────

def test_review_stats_say_what_a_bulk_publish_may_post(rid, db_path):
    _review(rid, db_path, "g/ready")
    _review(rid, db_path, "g/flagged", draft_needs_review=1)
    stats, status = client_api._do_review_stats(rid)
    assert status == 200
    assert stats["publishable"] == models.reply_queue_counts(rid)["publishable"] == 1
    assert stats["publish_held"] == 1


# ── the remove-competitor plan gate ─────────────────────────────────────────

def _user(rid):
    return {"id": 7, "restaurant_id": rid, "role": "owner", "email": "e@x.test"}


def test_removing_a_competitor_needs_the_plan_adding_one_does(rid, db_path):
    models.update_restaurant(rid, {"custom_competitors": "c1,c2", "module_reviews": 1, "module_labor": 0,
                                   "module_inventory": 0, "module_marketing": 0})
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/intel/remove-competitor", method="POST", json={"place_id": "c1"}):
        resp = mobile_api.mobile_remove_competitor.__wrapped__(current_user=_user(rid))
    body, status = (resp[0].get_json(), resp[1]) if isinstance(resp, tuple) else (resp.get_json(), resp.status_code)
    assert status == 403 and "Full System" in body["error"]
    assert models.get_restaurant(rid).custom_competitors == "c1,c2", "nothing changed"
    src = _read("mobile_api.py")
    rm = src[src.index("def mobile_remove_competitor("):]
    rm = rm[:rm.index("\n@mobile_bp.route")]
    assert "restaurant.module_reviews and restaurant.module_labor" in rm


# ── #75 the live check is a job ─────────────────────────────────────────────

@pytest.mark.parametrize("twin", ["web", "mobile"])
def test_an_async_check_starts_a_job_and_a_plain_one_answers_as_before(rid, monkeypatch, twin):
    started = []

    def fake_start(kind, restaurant_id, request_key, fn, *args, by_user=None, **kwargs):
        started.append((kind, restaurant_id, fn, args, by_user))
        return "job123", False
    monkeypatch.setattr(ai_async, "start", fake_start)
    monkeypatch.setattr(client_api, "_do_ai_visibility", lambda r, force=False: ({"ok": True, "force": force}, 200))
    monkeypatch.setattr(client_api, "present_ai_visibility_roadmap", lambda r, p, uid=None: dict(p, presented=True))
    route = client_api.ai_visibility if twin == "web" else mobile_api.mobile_ai_visibility
    path = "/api/ai-visibility" if twin == "web" else "/mobile/api/intel/ai-visibility"
    app = Flask(__name__)
    with app.test_request_context(path, method="POST", json={"async": 1}):
        resp, status = route.__wrapped__(current_user=_user(rid))
    body = resp.get_json()
    assert status == 202 and body["job_id"] == "job123" and body["async"] is True
    assert body["wait_seconds"] == ai_async.JOB_SECONDS["ai_visibility"]
    kind, r, fn, args, by_user = started[0]
    assert (kind, r, by_user) == ("ai_visibility", rid, 7)
    # The job runs the very body the synchronous route answers with.
    assert fn(*args) == ({"ok": True, "force": True, "presented": True}, 200)
    # Without the flag (an older app) the check answers in the request.
    with app.test_request_context(path, method="POST", json={}):
        resp, status = route.__wrapped__(current_user=_user(rid))
    assert status == 200 and resp.get_json() == {"ok": True, "force": True, "presented": True}
    # A GET is a read, never a job.
    with app.test_request_context(path + "?async=1", method="GET"):
        resp, status = route.__wrapped__(current_user=_user(rid))
    assert resp.get_json()["force"] is False and len(started) == 1


def test_the_web_check_polls_the_job():
    dash = _read("templates/dashboard.html")
    run = dash[dash.index("fetch('/api/ai-visibility', {method: 'POST'"):]
    run = run[:run.index("\n}\n")]
    assert "JSON.stringify({async: 1})" in run and "aiJobAwait(d)" in run


# ── #91 Approve after all ───────────────────────────────────────────────────

def test_a_person_may_approve_a_reply_they_skipped_but_the_rule_never_does(rid, db_path):
    a = _review(rid, db_path, "g/skipped-a", status="skipped")
    b = _review(rid, db_path, "g/skipped-b", status="skipped")
    person = {"user_id": 7, "role": "principal", "via": "normal"}
    assert models.claim_approval(a, rid, approver=person) is True
    assert _rows(rid)[a]["response_status"] == "approved"
    assert models.claim_approval(b, rid, approver=models.reply_approver(auto=True)) is False
    assert _rows(rid)[b]["response_status"] == "skipped"
    # Nor in a bulk publish: BULK_PUBLISHABLE_SQL is drafted-only.
    assert models.claim_approval(b, rid, publishable_only=True, approver=person) is False


# ── #52 Read now on the web ─────────────────────────────────────────────────

def test_the_web_connect_card_has_read_now():
    dash = _read("templates/dashboard.html")
    render = dash[dash.index("function waRender(checks){"):dash.index("function waChecksHtml(checks){")]
    assert 'onclick="waSync(this)">Read now</button>' in render
    sync = dash[dash.index("function waSync(btn){"):]
    sync = sync[:sync.index("\n}\n")]
    assert "fetch('/api/web-analytics/sync',{method:'POST'" in sync and "cbtnBusy(btn" in sync
