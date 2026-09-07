"""The marketing module's new machinery: media, scheduling, drafts, links,
segments, newsletter, attribution and preview.

Each of these exists because the module could previously only do one thing at
one moment — generate now, post now, to everyone. The tests below pin the
behaviour that makes each of them worth having, not just that the code runs.
"""
import io
from datetime import datetime, timedelta

import pytest

from time_utils import restaurant_now


def _now() -> datetime:
    """Every restaurant this file creates is timezone="America/Chicago" and
    marketing_publish.py's due-ness check (_local_now) compares against that
    restaurant's own local time via ZoneInfo — never the test runner's system
    clock. Plain _now() happened to agree with that on a developer
    Mac already set to America/Chicago, which is exactly how this masked
    itself for months: every one of these tests passed locally and failed on
    GitHub's UTC runner, since "5 hours from now" meant two different moments
    depending on which timezone _now() actually read. Root-caused
    Sep 7 2026 chasing the same CI-failure-email investigation that fixed
    value_delivered.py and notify.py."""
    return restaurant_now("America/Chicago", naive=True)

import guest_email
import guest_marketing
import marketing_drafts
import marketing_links
import marketing_media
import marketing_publish
import marketing_signals
import models
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """Every one of these modules does `from models import get_conn`, so each
    holds its own reference and has to be redirected by name."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, guest_marketing, guest_email, marketing_media,
                marketing_publish, marketing_drafts, marketing_links, marketing_signals):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    guest_marketing.init_guest_marketing(db_path)
    # marketing_drafts joins `users` to show who wrote and who approved.
    # conftest's db_path only runs models.init_db, and users lives in auth's
    # own schema — the real app boot creates both.
    import auth
    conn = real(db_path)
    conn.executescript(auth.AUTH_SCHEMA)
    conn.commit()
    conn.close()


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(
        name="Feature Co", owner_email="f@x.com", module_marketing=1,
        timezone="America/Chicago"), db_path=db_path)


def _connect_all(db_path, rid):
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET ig_token='t', ig_user_id='u', "
                 "fb_page_token='t', fb_page_id='p', gmb_refresh_token='r', "
                 "gmb_account_id='accounts/1', gmb_location_id='locations/1' WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def _png(size=(60, 40), color=(200, 75, 47)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


# ── Photos ─────────────────────────────────────────────────────────────────

def test_an_uploaded_photo_comes_back_out_as_a_fetchable_jpeg(rid, db_path):
    """Meta fetches this URL at publish time; HEIC straight off an iPhone is
    the common upload and Meta will not take it."""
    stored = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    found = marketing_media.get_image(stored["token"], db_path=db_path)
    assert found is not None
    data, mime = found
    assert mime == "image/jpeg"
    assert data[:2] == b"\xff\xd8"          # JPEG magic
    assert marketing_media.media_url("https://x.test/", stored["token"]).endswith(".jpg")


def test_a_large_photo_is_downscaled_rather_than_stored_whole(rid, db_path):
    stored = marketing_media.store_image(rid, _png(size=(4032, 3024)), "image/png", db_path=db_path)
    assert max(stored["width"], stored["height"]) == marketing_media.MAX_EDGE
    assert stored["size_bytes"] < 1_500_000


def test_a_file_that_is_not_a_photo_is_refused_with_something_actionable(rid, db_path):
    with pytest.raises(marketing_media.MediaError):
        marketing_media.store_image(rid, b"not an image at all", "image/png", db_path=db_path)


def test_one_restaurant_cannot_address_anothers_photo_by_id(db_path, rid):
    other = create_restaurant(Restaurant(name="Other", owner_email="o@x.com"), db_path=db_path)
    stored = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    assert marketing_media.get_media_token(stored["id"], other, db_path=db_path) is None
    assert marketing_media.get_media_token(stored["id"], rid, db_path=db_path) == stored["token"]


# ── Scheduling ─────────────────────────────────────────────────────────────

def _in_hours(h):
    return (_now() + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S")


def test_a_post_can_be_queued_for_later(rid, db_path):
    _connect_all(db_path, rid)
    result = marketing_publish.schedule_post(rid, "facebook", "Patio is open", _in_hours(4),
                                             db_path=db_path)
    assert result["ok"]
    assert [p["status"] for p in marketing_publish.list_scheduled(rid, db_path=db_path)] == ["scheduled"]


def test_a_time_in_the_past_is_refused_rather_than_published_immediately(rid, db_path):
    """Silently posting now when the owner asked for Tuesday is worse than
    saying no."""
    _connect_all(db_path, rid)
    result = marketing_publish.schedule_post(rid, "facebook", "Hi", _in_hours(-3), db_path=db_path)
    assert not result["ok"]
    assert "passed" in result["error"]


def test_instagram_cannot_be_scheduled_without_a_photo(rid, db_path):
    _connect_all(db_path, rid)
    result = marketing_publish.schedule_post(rid, "instagram", "Hi", _in_hours(2), db_path=db_path)
    assert not result["ok"]
    assert "photo" in result["error"]


def test_a_disconnected_platform_cannot_be_scheduled_to(rid, db_path):
    result = marketing_publish.schedule_post(rid, "facebook", "Hi", _in_hours(2), db_path=db_path)
    assert not result["ok"]
    assert "connected" in result["error"]


def test_a_due_post_publishes_and_one_that_is_not_due_waits(rid, db_path, monkeypatch):
    _connect_all(db_path, rid)
    due = marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    marketing_publish.schedule_post(rid, "facebook", "Later", _in_hours(30), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": True, "post_id": "fb_1"}, 200))

    result = marketing_publish.run_due_posts(db_path=db_path)

    assert (result["published"], result["pending"]) == (1, 1)
    rows = {p["id"]: p for p in marketing_publish.list_scheduled(rid, db_path=db_path)}
    assert rows[due["id"]]["status"] == "posted"
    assert rows[due["id"]]["post_id"] == "fb_1"


def test_a_post_that_missed_its_slot_by_hours_is_failed_not_published(rid, db_path, monkeypatch):
    """A brunch post landing at dinner is worse than one that didn't land."""
    _connect_all(db_path, rid)
    created = marketing_publish.schedule_post(rid, "facebook", "Brunch", _in_hours(1), db_path=db_path)
    conn = get_conn(db_path)
    stale = (_now() - timedelta(hours=marketing_publish.LATE_TOLERANCE_HOURS + 2))
    conn.execute("UPDATE marketing_scheduled_posts SET scheduled_for=? WHERE id=?",
                 (stale.strftime("%Y-%m-%dT%H:%M:%S"), created["id"]))
    conn.commit(); conn.close()
    sent = []
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: sent.append(1) or ({"ok": True, "post_id": "x"}, 200))

    marketing_publish.run_due_posts(db_path=db_path)

    assert sent == []
    assert marketing_publish.list_scheduled(rid, db_path=db_path)[0]["status"] == "failed"


def test_a_transient_failure_is_retried_before_the_post_is_given_up_on(rid, db_path, monkeypatch):
    _connect_all(db_path, rid)
    marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": False, "error": "Meta 500"}, 200))

    for _ in range(marketing_publish.MAX_ATTEMPTS - 1):
        marketing_publish.run_due_posts(db_path=db_path)
        assert marketing_publish.list_scheduled(rid, db_path=db_path)[0]["status"] == "scheduled"

    marketing_publish.run_due_posts(db_path=db_path)
    assert marketing_publish.list_scheduled(rid, db_path=db_path)[0]["status"] == "failed"


def test_a_cancelled_post_never_goes_out(rid, db_path, monkeypatch):
    _connect_all(db_path, rid)
    created = marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    assert marketing_publish.cancel_scheduled(created["id"], rid, db_path=db_path)["ok"]
    sent = []
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: sent.append(1) or ({"ok": True, "post_id": "x"}, 200))

    marketing_publish.run_due_posts(db_path=db_path)

    assert sent == []


def test_one_restaurant_cannot_cancel_anothers_post(db_path, rid):
    _connect_all(db_path, rid)
    other = create_restaurant(Restaurant(name="Other", owner_email="o@x.com"), db_path=db_path)
    created = marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(3), db_path=db_path)
    assert not marketing_publish.cancel_scheduled(created["id"], other, db_path=db_path)["ok"]


# ── Preview ────────────────────────────────────────────────────────────────

def test_preview_catches_a_draft_that_still_has_both_options_in_it():
    """The single worst thing this module could publish, caught before it
    reaches a real feed rather than after."""
    result = marketing_publish.preview(
        "instagram", "Option 1 (Short & Punchy): come by\n\nOption 2 (Storytelling): really come by",
        media_token="tok")
    assert not result["ready"]
    assert any("both drafts" in p for p in result["problems"])


def test_preview_reports_the_platform_ceiling(rid):
    result = marketing_publish.preview("google", "x" * 1600)
    assert result["over_limit"]
    assert "1,500" in result["problems"][0]


def test_preview_says_instagram_needs_a_photo():
    result = marketing_publish.preview("instagram", "A caption")
    assert any("photo" in p for p in result["problems"])


def test_preview_counts_sms_segments_rather_than_just_flagging_length():
    result = marketing_publish.preview("sms", "x" * 400)
    assert any("linked texts" in p for p in result["problems"])


# ── Drafts and approval ────────────────────────────────────────────────────

def test_a_draft_survives_leaving_the_screen(rid, db_path):
    saved = marketing_drafts.save_draft(rid, "Truffle season copy", topic="truffle", db_path=db_path)
    assert saved["ok"]
    drafts = marketing_drafts.list_drafts(rid, db_path=db_path)
    assert [d["body"] for d in drafts] == ["Truffle season copy"]
    assert drafts[0]["status"] == "draft"


def test_the_primary_login_can_approve_and_an_invited_teammate_cannot(rid, db_path):
    """The first cut of this gate required role == "owner", which meant NOBODY
    could approve: every restaurant's primary login is 'client' and 'owner' is
    reserved for the multi-restaurant account. auth.invite_team_member's own
    comment records the same mistake being made and fixed for Team access."""
    saved = marketing_drafts.save_draft(rid, "Copy", db_path=db_path)

    refused = marketing_drafts.approve_draft(saved["id"], rid, role="member", db_path=db_path)
    assert not refused["ok"]
    assert marketing_drafts.list_drafts(rid, db_path=db_path)[0]["status"] == "draft"

    allowed = marketing_drafts.approve_draft(saved["id"], rid, role="client", db_path=db_path)
    assert allowed["ok"]
    assert marketing_drafts.list_drafts(rid, db_path=db_path)[0]["status"] == "approved"


@pytest.mark.parametrize("role", ["client", "owner", None])
def test_every_real_account_role_can_approve(rid, db_path, role):
    """The regression guard: a role this codebase actually issues must never
    be locked out of releasing its own content."""
    saved = marketing_drafts.save_draft(rid, "Copy", db_path=db_path)
    assert marketing_drafts.approve_draft(saved["id"], rid, role=role, db_path=db_path)["ok"]


def test_editing_an_approved_draft_sends_it_back_for_approval(rid, db_path):
    """Otherwise "approved" means someone approved some earlier version of
    this, which is worse than no approval at all."""
    saved = marketing_drafts.save_draft(rid, "First", db_path=db_path)
    marketing_drafts.approve_draft(saved["id"], rid, role="client", db_path=db_path)
    marketing_drafts.save_draft(rid, "Rewritten", draft_id=saved["id"], db_path=db_path)
    assert marketing_drafts.list_drafts(rid, db_path=db_path)[0]["status"] == "draft"


def test_one_restaurant_cannot_read_or_approve_anothers_draft(db_path, rid):
    other = create_restaurant(Restaurant(name="Other", owner_email="o@x.com"), db_path=db_path)
    saved = marketing_drafts.save_draft(rid, "Private copy", db_path=db_path)
    assert marketing_drafts.list_drafts(other, db_path=db_path) == []
    assert not marketing_drafts.approve_draft(saved["id"], other, role="client", db_path=db_path)["ok"]


# ── Link tracking ──────────────────────────────────────────────────────────

def test_a_short_link_counts_the_tap_and_forwards(rid, db_path):
    made = marketing_links.create_link(rid, "example.com/menu", source="sms",
                                       campaign="truffle", db_path=db_path)
    assert made["ok"]
    target = marketing_links.resolve(made["token"], db_path=db_path)
    assert target.startswith("https://example.com/menu")
    assert "utm_source=cavnar_sms" in target
    assert marketing_links.link_stats(rid, db_path=db_path)[0]["clicks"] == 1


def test_a_link_the_owner_already_tagged_keeps_their_own_utms(rid, db_path):
    made = marketing_links.create_link(
        rid, "https://example.com/?utm_source=mine", source="sms", db_path=db_path)
    assert "utm_source=mine" in made["target_url"]
    assert "cavnar_sms" not in made["target_url"]


def test_a_non_web_target_is_refused(rid, db_path):
    """An open redirect that accepts anything is a phishing endpoint wearing
    the restaurant's domain."""
    assert not marketing_links.create_link(rid, "javascript:alert(1)", db_path=db_path)["ok"]
    assert not marketing_links.create_link(rid, "", db_path=db_path)["ok"]


def test_an_unknown_token_resolves_to_nothing(db_path):
    assert marketing_links.resolve("not-a-real-token", db_path=db_path) is None


# ── Segments ───────────────────────────────────────────────────────────────

def _guest(db_path, rid, phone, *, consent=1, unsubscribed=0, visits=0, days_ago=None):
    now = _now()
    last_visit = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S") if days_ago is not None else None
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO guest_contacts (restaurant_id, name, phone, consent, unsubscribed, "
        "visit_count, last_visit) VALUES (?,?,?,?,?,?,?)",
        (rid, phone, phone, consent, unsubscribed, visits, last_visit))
    conn.commit(); conn.close()


def test_a_win_back_text_does_not_reach_someone_who_ate_here_last_night(rid, db_path):
    """`win_back` used to be a TONE the copy was written in, never an
    AUDIENCE it was sent to."""
    _guest(db_path, rid, "+15550000001", visits=2, days_ago=1)
    _guest(db_path, rid, "+15550000002", visits=2, days_ago=45)

    lapsed = guest_marketing.segment_contacts(rid, "lapsed_30", db_path=db_path)

    assert [c["phone"] for c in lapsed] == ["+15550000002"]


def test_a_guest_with_no_recorded_visit_is_not_treated_as_lapsed(rid, db_path):
    """Joining at the table and never being marked is not the same as
    drifting away."""
    _guest(db_path, rid, "+15550000003", visits=0, days_ago=None)
    assert guest_marketing.segment_contacts(rid, "lapsed_30", db_path=db_path) == []


def test_regulars_and_first_timers_are_different_audiences(rid, db_path):
    _guest(db_path, rid, "+15550000004", visits=5, days_ago=3)
    _guest(db_path, rid, "+15550000005", visits=1, days_ago=3)
    assert [c["phone"] for c in guest_marketing.segment_contacts(rid, "regulars", db_path=db_path)] == ["+15550000004"]
    assert [c["phone"] for c in guest_marketing.segment_contacts(rid, "new", db_path=db_path)] == ["+15550000005"]


def test_a_segment_can_only_narrow_the_consented_set_never_widen_it(rid, db_path):
    _guest(db_path, rid, "+15550000006", consent=0, visits=9, days_ago=90)
    _guest(db_path, rid, "+15550000007", consent=1, unsubscribed=1, visits=9, days_ago=90)
    for segment in guest_marketing.SEGMENTS:
        assert guest_marketing.segment_contacts(rid, segment, db_path=db_path) == []


def test_an_unknown_segment_falls_back_to_everyone_rather_than_nobody(rid, db_path):
    _guest(db_path, rid, "+15550000008", visits=1, days_ago=2)
    assert len(guest_marketing.segment_contacts(rid, "made-up", db_path=db_path)) == 1


# ── Frequency cap and campaign history ─────────────────────────────────────

@pytest.fixture
def _inside_hours(monkeypatch):
    monkeypatch.setattr(guest_marketing, "_sms_local_now",
                        lambda r: _now().replace(hour=12, minute=0))


def test_nobody_gets_two_campaigns_inside_three_days(rid, db_path, monkeypatch, _inside_hours):
    _guest(db_path, rid, "+15550000009", visits=1, days_ago=2)
    monkeypatch.setattr(guest_marketing, "send_sms", lambda *a, **k: True)

    first = guest_marketing.send_campaign(rid, "One", db_path=db_path)
    second = guest_marketing.send_campaign(rid, "Two", db_path=db_path)

    assert first["sent"] == 1
    assert second["sent"] == 0
    assert second["skipped_recent"] == 1


def test_a_campaign_records_who_it_went_to(rid, db_path, monkeypatch, _inside_hours):
    _guest(db_path, rid, "+15550000010", visits=4, days_ago=40)
    monkeypatch.setattr(guest_marketing, "send_sms", lambda *a, **k: True)

    guest_marketing.send_campaign(rid, "Come back", segment="lapsed_30", db_path=db_path)

    history = guest_marketing.campaign_history(rid, db_path=db_path)
    assert history[0]["segment"] == "lapsed_30"
    assert history[0]["segment_label"] == "Haven't been in 30+ days"
    assert history[0]["sent_count"] == 1


def test_a_campaign_can_carry_a_tracked_link(rid, db_path, monkeypatch, _inside_hours):
    _guest(db_path, rid, "+15550000011", visits=1, days_ago=2)
    bodies = []
    monkeypatch.setattr(guest_marketing, "send_sms", lambda phone, body: bodies.append(body) or True)
    made = marketing_links.create_link(rid, "https://example.com/menu", db_path=db_path)

    guest_marketing.send_campaign(rid, "Menu is live", link_token=made["token"], db_path=db_path)

    assert f"/g/{made['token']}" in bodies[0]
    assert "Reply STOP" in bodies[0]


def test_the_consent_ledger_answers_how_a_number_got_on_this_list(rid, db_path):
    _guest(db_path, rid, "+15550000012", consent=1, visits=1, days_ago=1)
    _guest(db_path, rid, "+15550000013", consent=1, unsubscribed=1)
    _guest(db_path, rid, "+15550000014", consent=0)

    ledger = guest_marketing.consent_ledger(rid, db_path=db_path)

    assert (ledger["total"], ledger["textable"], ledger["unsubscribed"], ledger["no_consent"]) == (3, 1, 1, 1)
    assert "8:00 AM" in ledger["window"]


# ── Newsletter ─────────────────────────────────────────────────────────────

def test_the_generated_subject_line_block_never_reaches_a_guest():
    """marketing.py's weekly_email prompt returns "SUBJECT LINE: (2 options)"
    then "BODY:" — sending that verbatim mails guests the scaffolding."""
    subject, body = guest_email._split_generated(
        "SUBJECT LINE:\n1. Truffle season starts Friday\n2. The pie you wait for\n\n"
        "BODY:\nHey — truffle season is here.\n\n— Will")
    assert subject == "Truffle season starts Friday"
    assert "SUBJECT" not in body and "The pie you wait for" not in body
    assert body.startswith("Hey")


def test_a_letter_with_no_markers_is_left_alone(rid):
    subject, body = guest_email._split_generated("Just a plain letter.\n\nCome by.")
    assert subject == ""
    assert body == "Just a plain letter.\n\nCome by."


def test_an_address_given_without_the_box_ticked_is_never_mailed(rid, db_path):
    """Ticking the SMS consent box is not agreement to a newsletter."""
    cid = guest_marketing.add_guest_contact_public_optin(rid, "+15550000015", db_path=db_path)
    guest_email.set_guest_email(cid, rid, "guest@example.com", consent=False, db_path=db_path)
    assert guest_email.subscribers(rid, db_path=db_path) == []

    guest_email.set_guest_email(cid, rid, "guest@example.com", consent=True, db_path=db_path)
    assert [s["email"] for s in guest_email.subscribers(rid, db_path=db_path)] == ["guest@example.com"]


def test_a_newsletter_unsubscribe_works_without_signing_in(rid, db_path):
    cid = guest_marketing.add_guest_contact_public_optin(rid, "+15550000016", db_path=db_path)
    guest_email.set_guest_email(cid, rid, "guest@example.com", consent=True, db_path=db_path)
    token = guest_email.subscribers(rid, db_path=db_path)[0]["email_token"]

    assert guest_email.unsubscribe(token, db_path=db_path) == "Feature Co"
    assert guest_email.subscribers(rid, db_path=db_path) == []


def test_a_newsletter_with_no_subscribers_says_so_instead_of_sending_nothing(rid, db_path):
    result = guest_email.send_newsletter(rid, "Hello", db_path=db_path)
    assert not result["ok"]
    assert "opted in" in result["error"]


# ── Attribution ────────────────────────────────────────────────────────────

def _post(db_path, rid, days_ago, topic="Post"):
    at = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, "
        "post_platform, posted_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (rid, "instagram_post", topic, f"ig_{days_ago}", "instagram", at, at))
    conn.commit(); post_id = cur.lastrowid; conn.close()
    return post_id


def test_attribution_says_nothing_when_there_is_no_pos_data(rid, db_path, monkeypatch):
    """Saying nothing is the correct output far more often than a number is."""
    monkeypatch.setattr(marketing_signals, "daily_sales", lambda r: {})
    post_id = _post(db_path, rid, days_ago=2)
    assert marketing_signals.attribution_for_post(rid, post_id, db_path=db_path) == {
        "ok": False, "reason": "no_pos_data"}


def test_attribution_says_nothing_without_enough_comparable_history(rid, db_path, monkeypatch):
    today = _now()
    monkeypatch.setattr(marketing_signals, "daily_sales",
                        lambda r: {today.strftime("%Y-%m-%d"): 5000.0})
    post_id = _post(db_path, rid, days_ago=0)
    assert marketing_signals.attribution_for_post(rid, post_id, db_path=db_path)["reason"] == "not_enough_history"


def test_attribution_compares_a_post_against_the_same_weekday_before_it(rid, db_path, monkeypatch):
    """Comparing a Friday post to a Tuesday would just measure the weekend."""
    posted = _now() - timedelta(days=1)
    sales = {}
    for week in range(1, marketing_signals.BASELINE_WEEKS + 1):
        for i in range(2):
            d = (posted + timedelta(days=i) - timedelta(weeks=week)).strftime("%Y-%m-%d")
            sales[d] = 1000.0
    for i in range(2):
        sales[(posted + timedelta(days=i)).strftime("%Y-%m-%d")] = 1200.0
    monkeypatch.setattr(marketing_signals, "daily_sales", lambda r: sales)
    post_id = _post(db_path, rid, days_ago=1)

    result = marketing_signals.attribution_for_post(rid, post_id, db_path=db_path)

    assert result["ok"]
    assert result["lift_pct"] == 20.0
    assert result["baseline_days"] >= marketing_signals.MIN_BASELINE_DAYS


# ── Windowed analytics ─────────────────────────────────────────────────────

def test_performance_is_reported_for_a_window_and_against_the_one_before(rid, db_path):
    """"Total reach 4,231" with no denominator and no trend is a number, not
    a metric."""
    conn = get_conn(db_path)
    for days_ago, reach, likes in ((3, 1000, 100), (40, 500, 25)):
        at = (_now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO marketing_content_log (restaurant_id, topic, post_id, post_platform, "
            "reach, likes, posted_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (rid, f"p{days_ago}", f"ig{days_ago}", "instagram", reach, likes, at, at))
    conn.commit(); conn.close()

    window = marketing_signals.performance_window(rid, days=30, db_path=db_path)

    assert window["posts"] == 1
    assert window["reach"] == 1000
    assert window["engagement_rate"] == 10.0
    assert window["previous"]["posts"] == 1
    assert window["change"]["reach"] == 100.0
    assert window["by_platform"][0]["platform"] == "instagram"


def test_a_post_published_today_counts_toward_this_window(rid, db_path):
    """date('now','-0 days') is today's date, and a row stamped today is not
    < today — so a post published this morning was missing from the headline
    numbers while still showing up in the per-platform breakdown."""
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO marketing_content_log (restaurant_id, topic, post_id, post_platform, "
        "reach, likes, posted_at, created_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (rid, "today", "ig_today", "instagram", 800, 40))
    conn.commit(); conn.close()

    window = marketing_signals.performance_window(rid, days=30, db_path=db_path)

    assert window["posts"] == 1
    assert window["reach"] == 800
    assert window["posts"] == sum(p["posts"] for p in window["by_platform"])


def test_regenerating_the_same_topic_does_not_contradict_the_brief(rid, db_path, monkeypatch):
    """Pressing Regenerate handed the model "you have recently generated
    content about X. Do NOT repeat these themes" while the instruction above
    said to write about X — and it answered the contradiction instead of the
    brief ("Since I already have two prior posts...")."""
    import marketing
    monkeypatch.setattr(marketing, "get_recent_content", lambda r, limit=5: [
        {"type": "instagram_post", "topic": "fall truffle menu"},
        {"type": "weekly_email", "topic": "Sunday brunch"},
    ])
    monkeypatch.setattr(marketing, "log_content", lambda *a, **k: None)
    monkeypatch.setattr(marketing, "generation_context", lambda r: "", raising=False)
    captured = {}

    def _spy(client, **kw):
        captured["prompt"] = kw["messages"][0]["content"]
        return "copy"

    monkeypatch.setattr(marketing, "create_with_retry", _spy)
    monkeypatch.setattr(marketing, "extract_text", lambda m: m)

    marketing.generate_content("instagram_post", "fall truffle menu", restaurant_id=rid)

    prompt = captured["prompt"]
    assert "Topic/occasion: fall truffle menu" in prompt
    # The topic just asked for is not also listed as something to avoid.
    avoid = prompt.split("Do NOT repeat these themes")[0].split("recently generated content about:")[-1]
    assert "fall truffle menu" not in avoid
    assert "Sunday brunch" in avoid


# ── Failure alerting ───────────────────────────────────────────────────────
# A scheduled post that fails is invisible: the owner planned it days ago and
# it simply never appeared. Nothing surfaced that except the queue screen,
# which is the one place they have no reason to look.

def test_the_owner_is_told_when_a_post_finally_fails(rid, db_path, monkeypatch):
    _connect_all(db_path, rid)
    marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": False, "error": "Facebook not connected"}, 200))
    alerts = []
    monkeypatch.setattr("notify._send_alert_email",
                        lambda email, subject, html, restaurant_id=None:
                            alerts.append((subject, html)) or True)

    for _ in range(marketing_publish.MAX_ATTEMPTS):
        marketing_publish.run_due_posts(db_path=db_path)

    assert len(alerts) == 1, "one alert, when it fails for good"
    subject, html = alerts[0]
    assert "didn't go out" in subject
    assert "Reconnect it under Account → Connections" in html


def test_the_retries_before_that_are_silent(rid, db_path, monkeypatch):
    """Alerting on every attempt would train the owner to ignore it."""
    _connect_all(db_path, rid)
    marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": False, "error": "Meta 500"}, 200))
    alerts = []
    monkeypatch.setattr("notify._send_alert_email",
                        lambda *a, **k: alerts.append(1) or True)

    marketing_publish.run_due_posts(db_path=db_path)

    assert alerts == []
    assert marketing_publish.list_scheduled(rid, db_path=db_path)[0]["status"] == "scheduled"


def test_a_post_that_missed_its_slot_also_tells_the_owner(rid, db_path, monkeypatch):
    _connect_all(db_path, rid)
    created = marketing_publish.schedule_post(rid, "facebook", "Brunch", _in_hours(1), db_path=db_path)
    conn = get_conn(db_path)
    stale = _now() - timedelta(hours=marketing_publish.LATE_TOLERANCE_HOURS + 2)
    conn.execute("UPDATE marketing_scheduled_posts SET scheduled_for=? WHERE id=?",
                 (stale.strftime("%Y-%m-%dT%H:%M:%S"), created["id"]))
    conn.commit(); conn.close()
    alerts = []
    monkeypatch.setattr("notify._send_alert_email",
                        lambda email, subject, html, restaurant_id=None:
                            alerts.append(html) or True)

    marketing_publish.run_due_posts(db_path=db_path)

    assert len(alerts) == 1
    assert "Reschedule it" in alerts[0]


def test_an_alert_that_throws_does_not_take_down_the_queue_runner(rid, db_path, monkeypatch):
    """The post still has to be marked failed even if the email blows up."""
    _connect_all(db_path, rid)
    marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": False, "error": "nope"}, 200))
    monkeypatch.setattr("notify._send_alert_email",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SMTP down")))

    for _ in range(marketing_publish.MAX_ATTEMPTS):
        result = marketing_publish.run_due_posts(db_path=db_path)

    assert result["failed"] == 1
    assert marketing_publish.list_scheduled(rid, db_path=db_path)[0]["status"] == "failed"


def test_repeated_failures_reach_the_operator_digest(rid, db_path, monkeypatch):
    """One failure is the owner's problem. A run where several fail is usually
    one cause, and that belongs across restaurants rather than per-inbox."""
    _connect_all(db_path, rid)
    for _ in range(2):
        marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": False, "error": "token expired"}, 200))
    monkeypatch.setattr("notify._send_alert_email", lambda *a, **k: True)
    captured = []
    monkeypatch.setattr("ops.capture",
                        lambda exc, job=None, context="": captured.append((job, str(exc), context)))

    for _ in range(marketing_publish.MAX_ATTEMPTS):
        marketing_publish.run_due_posts(db_path=db_path)

    assert captured, "nothing reached ops"
    job, message, context = captured[-1]
    assert job == "scheduled_posts"
    assert "failed to publish" in message
    assert "failed=2" in context


def test_a_successful_run_stays_silent(rid, db_path, monkeypatch):
    _connect_all(db_path, rid)
    marketing_publish.schedule_post(rid, "facebook", "Now", _in_hours(0), db_path=db_path)
    monkeypatch.setattr("social_routes._do_post_to_facebook",
                        lambda *a, **k: ({"ok": True, "post_id": "fb_1"}, 200))
    noise = []
    monkeypatch.setattr("notify._send_alert_email", lambda *a, **k: noise.append("email"))
    monkeypatch.setattr("ops.capture", lambda *a, **k: noise.append("ops"))

    marketing_publish.run_due_posts(db_path=db_path)

    assert noise == [], "silence has to stay meaningful"


# ── Caption post-processing ────────────────────────────────────────────────

def test_hashtags_survive_the_markdown_stripper(rid, db_path, monkeypatch):
    """The stripper removed a leading "#" with an OPTIONAL space after it, so
    a caption whose hashtags started their own line lost the "#" off the first
    one — "#GiaMia #TruffleSeason" went out as "GiaMia #TruffleSeason". One
    broken tag on every Instagram post, silently."""
    import marketing
    monkeypatch.setattr(marketing, "get_recent_content", lambda r, limit=5: [])
    monkeypatch.setattr(marketing, "log_content", lambda *a, **k: None)
    monkeypatch.setattr(marketing, "generation_context", lambda r: "", raising=False)
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **kw: None)
    monkeypatch.setattr(
        marketing, "extract_text",
        lambda m: "Truffle season just landed.\n\n#GiaMia #TruffleSeason #WoodFired")

    out = marketing.generate_content("instagram_post", "truffle", restaurant_id=rid)

    assert "#GiaMia" in out
    assert out.count("#") == 3


def test_a_real_markdown_heading_is_still_stripped(rid, db_path, monkeypatch):
    import marketing
    monkeypatch.setattr(marketing, "get_recent_content", lambda r, limit=5: [])
    monkeypatch.setattr(marketing, "log_content", lambda *a, **k: None)
    monkeypatch.setattr(marketing, "generation_context", lambda r: "", raising=False)
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **kw: None)
    monkeypatch.setattr(marketing, "extract_text", lambda m: "## Caption\nCome by tonight.")

    out = marketing.generate_content("instagram_post", "x", restaurant_id=rid)

    assert out.startswith("Caption")
