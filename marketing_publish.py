"""marketing_publish.py — one publish path, and the queue in front of it.

Two things live here that used to be missing entirely.

First, ONE place that actually publishes. Instagram, Facebook and Google each
had their own route on web, their own route on mobile, and their own copy of
"post it, then log it" — so a scheduled post would have needed a fourth. Now
every caller lands on `publish_now`, and the content log is written once.

Second, the queue. Everything this module did was generate-now/post-now, and
a restaurant owner does admin at 11pm. The content calendar would tell them to
post Tuesday lunch and then require them to be standing in the office on
Tuesday at lunch. `schedule_post` writes the intent; `run_due_posts`, called
from scheduler.py's tick, publishes it.

Scheduled times are the restaurant's own wall clock — an owner picking
"Tuesday 11am" means 11am in their dining room — matching every other
time-of-day column in this schema.
"""
import logging
from datetime import datetime, timedelta

from models import get_conn, get_restaurant, DB_PATH

log = logging.getLogger(__name__)

PLATFORMS = ("instagram", "facebook", "google")

# How long after its slot a post may still go out. The scheduler ticks every
# few minutes, but a deploy or an outage can swallow a window — and a brunch
# post landing at 4pm is worse than one that didn't land, so a badly late post
# is failed rather than published.
LATE_TOLERANCE_HOURS = 6

MAX_ATTEMPTS = 3


def _local_now(restaurant_id):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(restaurant_id, naive=True)


def channels_for(restaurant_id, db_path: str = DB_PATH) -> dict:
    """Which destinations this restaurant can actually publish to."""
    r = get_restaurant(restaurant_id, db_path=db_path) if db_path != DB_PATH else get_restaurant(restaurant_id)
    return {
        "instagram": bool(r and getattr(r, "ig_token", None) and getattr(r, "ig_user_id", None)),
        "facebook": bool(r and getattr(r, "fb_page_token", None) and getattr(r, "fb_page_id", None)),
        "google": bool(r and getattr(r, "gmb_refresh_token", None)
                       and getattr(r, "gmb_account_id", None)
                       and getattr(r, "gmb_location_id", None)),
    }


# ── Publishing ─────────────────────────────────────────────────────────────

def publish_now(restaurant_id, platform, body, *, topic="", media_token=None,
                cta_type=None, cta_url=None, base_url="https://dashboard.cavnar.ai",
                content_type=None, scheduled_post_id=None, link_token=None,
                db_path: str = DB_PATH) -> dict:
    """Publish to one platform and log it. {"ok": True, "post_id": ...} or
    {"ok": False, "error": "..."} — never raises, so a queue runner can record
    the failure and move on."""
    platform = (platform or "").lower().strip()
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "There's no post text to publish.", "reached_platform": False}

    try:
        if platform == "instagram":
            from social_routes import _do_post_to_instagram
            image_url = _media_url(base_url, media_token)
            if not image_url:
                return {"ok": False, "error": "Instagram needs a photo — add one before posting.", "reached_platform": False}
            payload, _ = _do_post_to_instagram(restaurant_id, body, image_url, topic)
        elif platform == "facebook":
            from social_routes import _do_post_to_facebook
            payload, _ = _do_post_to_facebook(restaurant_id, body, topic)
        elif platform == "google":
            import gmb
            if not gmb.is_connected(restaurant_id):
                return {"ok": False, "error": "Google Business isn't connected.", "reached_platform": False}
            result = gmb.create_local_post(restaurant_id, body,
                                           cta_type=cta_type or None, cta_url=cta_url or None)
            payload = {"ok": bool(result.get("ok")),
                       "post_id": result.get("name"),
                       "error": result.get("error")}
        else:
            return {"ok": False, "error": f"Cavnar AI can't publish to {platform or 'that'}.", "reached_platform": False}
    except Exception as e:
        log.warning("publish_now %s failed for %s: %s", platform, restaurant_id, e)
        # A raised exception here is ambiguous: a timeout can mean the
        # platform never saw it, or saw it and accepted it. The caller must
        # not retry on this without a human looking.
        return {"ok": False, "error": "That platform rejected the post — try again in a moment.",
                "reached_platform": True}

    if not payload.get("ok"):
        # The platform answered and said no. Definite, so retrying is safe.
        return {"ok": False, "error": payload.get("error") or "The post didn't go through.",
                "reached_platform": False}

    post_id = payload.get("post_id")
    # Instagram and Facebook log their own content row inside social_routes;
    # Google's doesn't, and neither records the scheduling/media provenance —
    # so the row is written (or completed) here, in the one place that knows
    # all of it.
    _log_published(restaurant_id, content_type or _default_type(platform), topic or body[:80],
                   post_id, platform, scheduled_post_id=scheduled_post_id,
                   link_token=link_token, db_path=db_path)
    return {"ok": True, "post_id": post_id}


def _default_type(platform):
    return "google_promo" if platform == "google" else "instagram_post"


def _media_url(base_url, token):
    if not token:
        return None
    from marketing_media import media_url
    return media_url(base_url, token)


def _log_published(restaurant_id, content_type, topic, post_id, platform,
                   scheduled_post_id=None, link_token=None, db_path: str = DB_PATH):
    """One content-log row per published piece.

    social_routes already inserts a row for Instagram and Facebook, so this
    updates that row rather than creating a duplicate that would double every
    "pieces this month" count.
    """
    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM marketing_content_log WHERE restaurant_id=? AND post_id=? LIMIT 1",
            (restaurant_id, post_id),
        ).fetchone() if post_id else None
        if existing:
            conn.execute(
                "UPDATE marketing_content_log SET scheduled_post_id=?, link_token=?, "
                "posted_at=COALESCE(posted_at, datetime('now')) WHERE id=?",
                (scheduled_post_id, link_token, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO marketing_content_log "
                "(restaurant_id, content_type, topic, post_id, post_platform, "
                " scheduled_post_id, link_token, posted_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (restaurant_id, content_type, (topic or "")[:120], post_id, platform,
                 scheduled_post_id, link_token),
            )
        conn.commit()
    except Exception as e:
        log.warning("content log write failed for %s: %s", restaurant_id, e)
    finally:
        conn.close()


# ── Preview ────────────────────────────────────────────────────────────────
# You could write a 2,400-character Instagram caption and find out it was over
# the limit from Meta, after pressing Post. This is what the copy will
# actually look like where it lands, computed the same way on both platforms
# so they can't disagree about whether something fits.

PLATFORM_LIMITS = {
    "instagram": 2_200,
    "facebook": 63_206,
    "google": 1_500,
    "sms": 160,
    "email": None,
}

# Instagram collapses a caption after roughly this much and hides the rest
# behind "... more", which is where most captions actually lose people.
TRUNCATE_AT = {"instagram": 125, "facebook": 250, "google": 0, "sms": 0, "email": 0}


def preview(platform, body, *, media_token=None, cta_type=None, base_url=""):
    """What this post will look like, and whether it will be accepted."""
    platform = (platform or "").lower().strip()
    body = body or ""
    limit = PLATFORM_LIMITS.get(platform)
    length = len(body)

    problems = []
    if not body.strip():
        problems.append("There's no post text.")
    if limit and length > limit:
        problems.append(f"{length:,} characters — {platform.title()} caps at {limit:,}.")
    if platform == "instagram" and not media_token:
        problems.append("Instagram needs a photo.")
    if platform == "sms" and length > 160:
        segments = -(-length // 153)  # concatenated SMS parts are 153 chars each
        problems.append(f"Sends as {segments} linked texts rather than one.")

    # The prompts for instagram_post, loyalty_nudge and event_announcement ask
    # Claude for TWO versions. Publishing that untouched posts both, plus the
    # labels — the single worst thing this module could do, so it is caught
    # here rather than discovered on the restaurant's real feed.
    lowered = body.lower()
    if "option 1" in lowered and "option 2" in lowered:
        problems.append("This still has both drafts in it — keep the one you want.")

    cutoff = TRUNCATE_AT.get(platform) or 0
    visible = body[:cutoff] if cutoff and length > cutoff else body
    hashtags = [w for w in body.split() if w.startswith("#") and len(w) > 1]

    return {
        "platform": platform,
        "characters": length,
        "limit": limit,
        "over_limit": bool(limit and length > limit),
        "visible_before_more": visible,
        "truncated": bool(cutoff and length > cutoff),
        "hashtags": hashtags[:30],
        "hashtag_count": len(hashtags),
        "image_url": _media_url(base_url, media_token) if media_token else None,
        "cta": cta_type or None,
        "problems": problems,
        "ready": not problems,
    }


# ── The queue ──────────────────────────────────────────────────────────────

def schedule_post(restaurant_id, platform, body, scheduled_for, *, topic="",
                  content_type=None, media_id=None, cta_type=None, cta_url=None,
                  db_path: str = DB_PATH) -> dict:
    """Queue a post. `scheduled_for` is naive ISO in the restaurant's own local
    time. Refuses a slot in the past, since silently publishing something
    immediately when the owner asked for Tuesday is worse than saying no."""
    platform = (platform or "").lower().strip()
    if platform not in PLATFORMS:
        return {"ok": False, "error": f"Cavnar AI can't schedule posts to {platform or 'that'}."}
    body = (body or "").strip()
    if not body:
        return {"ok": False, "error": "There's no post text to schedule."}

    when = _parse_local(scheduled_for)
    if when is None:
        return {"ok": False, "error": "That date and time didn't make sense."}
    now = _local_now(restaurant_id)
    if when < now - timedelta(minutes=2):
        return {"ok": False, "error": "That time has already passed — pick a time from here on."}
    if when > now + timedelta(days=180):
        return {"ok": False, "error": "Scheduling only goes six months out."}

    if platform == "instagram" and not media_id:
        return {"ok": False, "error": "Instagram needs a photo — add one before scheduling."}

    if not channels_for(restaurant_id, db_path=db_path).get(platform):
        return {"ok": False, "error": f"{platform.title()} isn't connected yet."}

    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO marketing_scheduled_posts "
            "(restaurant_id, platform, content_type, topic, body, media_id, cta_type, cta_url, scheduled_for) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, platform, content_type, topic, body, media_id or None,
             cta_type or None, cta_url or None, when.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid,
                "scheduled_for": when.strftime("%Y-%m-%dT%H:%M:%S")}
    finally:
        conn.close()


def _parse_local(value):
    raw = str(value or "").strip().replace(" ", "T")
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw[:19] if len(raw) >= 19 else raw, fmt)
        except ValueError:
            continue
    return None


def list_scheduled(restaurant_id, include_done=True, limit=50, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        sql = ("SELECT s.*, m.token AS media_token FROM marketing_scheduled_posts s "
               "LEFT JOIN marketing_media m ON m.id = s.media_id "
               "WHERE s.restaurant_id=?")
        if not include_done:
            sql += " AND s.status='scheduled'"
        sql += " ORDER BY s.scheduled_for ASC LIMIT ?"
        rows = conn.execute(sql, (restaurant_id, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def cancel_scheduled(post_id, restaurant_id, db_path: str = DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        n = conn.execute(
            "UPDATE marketing_scheduled_posts SET status='cancelled' "
            "WHERE id=? AND restaurant_id=? AND status='scheduled'",
            (post_id, restaurant_id),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        return {"ok": False, "error": "That post has already gone out or was cancelled."}
    return {"ok": True}


def _explain_failure(platform, error):
    """Turn a platform's refusal into the thing the owner has to go do.

    A scheduled post that fails is invisible — the owner planned it days ago
    and it simply never appeared — so the alert has to say what broke AND
    what fixes it, not just repeat Meta's error string.
    """
    raw = (error or "").lower()
    if "not connected" in raw or "token" in raw or "expired" in raw or "oauth" in raw:
        return (f"{platform.title()} looks disconnected. Reconnect it under "
                "Account → Connections, then schedule the post again.")
    if "photo" in raw or "image" in raw or "media" in raw:
        return "The photo couldn't be used. Re-add it and schedule the post again."
    if "limit" in raw or "character" in raw:
        return "The post was over the platform's length limit. Trim it and schedule it again."
    if "missed its slot" in raw:
        return ("It wasn't published because too much time had passed — a lunch post "
                "landing at dinner is worse than none. Reschedule it when you're ready.")
    return "Open Marketing → Scheduled to see it and try again."


def _alert_failed_post(row, error, db_path: str = DB_PATH):
    """Tell the owner their post didn't go out.

    Fires once, when a post reaches `failed` for good — not on the retries in
    between, which are expected and usually recover. Best-effort: an alert
    that raises must never take down the queue runner behind it.
    """
    try:
        # db_path from the caller — this used to fall back to get_restaurant's
        # own module default, so on any database but the process-wide one it
        # emailed whoever happened to hold that id in the wrong file.
        restaurant = get_restaurant(row["restaurant_id"], db_path)
        if not restaurant or not restaurant.owner_email:
            return
        import notify
        platform = (row["platform"] or "").title()
        when = str(row["scheduled_for"] or "").replace("T", " ")[:16]
        body = (row["body"] or "").strip()
        preview = body[:120] + ("…" if len(body) > 120 else "")
        html = notify._alert_email_html(
            restaurant.name,
            f"Your {platform} post didn't go out",
            [
                f"It was scheduled for <strong>{when}</strong> and Cavnar AI couldn't publish it.",
                f"<em>{notify._html.escape(preview)}</em>",
                f"<strong>{notify._html.escape(str(error or 'The platform rejected it.'))}</strong>",
                _explain_failure(row["platform"] or "", error),
            ],
            cta_label="Open Marketing",
            restaurant_id=row["restaurant_id"],
        )
        notify._send_alert_email(
            restaurant.owner_email,
            f"Post didn't go out — {restaurant.name}",
            html,
            restaurant_id=row["restaurant_id"],
        )
    except Exception as e:
        log.warning("scheduled post failure alert failed for %s: %s", row["restaurant_id"], e)


def run_due_posts(base_url="https://dashboard.cavnar.ai", db_path: str = DB_PATH) -> dict:
    """Publish everything whose slot has arrived. Called from scheduler.py.

    Due-ness is computed per restaurant in ITS local time, because that is
    what the owner picked — two restaurants in different timezones asking for
    "11am" are two different moments.
    """
    # Anything a dying process left mid-publish is resolved before this pass
    # picks new work, so a stuck row can never be silently retried later.
    try:
        reap_stuck_publishes(db_path=db_path)
    except Exception as e:
        log.warning("reap_stuck_publishes failed: %s", e)

    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT s.*, m.token AS media_token FROM marketing_scheduled_posts s "
            "LEFT JOIN marketing_media m ON m.id = s.media_id "
            "WHERE s.status='scheduled' ORDER BY s.scheduled_for ASC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()

    published = failed = skipped = 0
    for row in rows:
        when = _parse_local(row["scheduled_for"])
        if when is None:
            _finish(row["id"], "failed", error="Unreadable scheduled time", db_path=db_path)
            failed += 1
            continue
        now = _local_now(row["restaurant_id"])
        if now < when:
            skipped += 1
            continue
        if now - when > timedelta(hours=LATE_TOLERANCE_HOURS):
            # A brunch post landing at dinner is worse than one that didn't land.
            late = f"Missed its slot by more than {LATE_TOLERANCE_HOURS} hours"
            _finish(row["id"], "failed", error=late, db_path=db_path)
            _alert_failed_post(row, late, db_path=db_path)
            failed += 1
            continue

        # Take the row before the network call, not after. Whoever wins this
        # owns the publish; a concurrent tick sees 'publishing' and moves on.
        if not _claim_for_publish(row["id"], db_path=db_path):
            skipped += 1
            continue

        result = publish_now(
            row["restaurant_id"], row["platform"], row["body"],
            topic=row["topic"] or "", media_token=row["media_token"],
            cta_type=row["cta_type"], cta_url=row["cta_url"], base_url=base_url,
            content_type=row["content_type"], scheduled_post_id=row["id"], db_path=db_path,
        )
        if result.get("ok"):
            _finish(row["id"], "posted", post_id=result.get("post_id"), db_path=db_path)
            published += 1
        elif result.get("reached_platform"):
            # The request went out and we never got a clean answer. It may
            # be live. Retrying could put a second copy on a public feed, so
            # this stops here and tells the owner rather than guessing.
            _finish(row["id"], "failed",
                    error=(result.get("error") or "Publish interrupted")
                          + " — it may already be live, check the platform before reposting.",
                    attempts=(row["attempts"] or 0) + 1, db_path=db_path)
            _alert_failed_post(row, "Publish interrupted — check the platform before reposting", db_path=db_path)
            failed += 1
        else:
            attempts = (row["attempts"] or 0) + 1
            # Nothing was sent, so retrying is safe — a transient Meta 500 or
            # a missing photo shouldn't burn the post.
            status = "failed" if attempts >= MAX_ATTEMPTS else "scheduled"
            _finish(row["id"], status, error=result.get("error"), attempts=attempts, db_path=db_path)
            if status == "failed":
                failed += 1
                # Once, on the way out — not on each retry.
                _alert_failed_post(row, result.get("error"), db_path=db_path)
    if failed:
        # Into the daily operator digest (ops.failures_last_24h). One post
        # failing is the owner's problem; a run where several fail is usually
        # one cause — an expired Meta token, a Google outage — and that is
        # worth seeing across restaurants rather than per-inbox.
        try:
            import ops
            ops.capture(RuntimeError(f"{failed} scheduled post(s) failed to publish"),
                        job="scheduled_posts",
                        context=f"published={published} failed={failed} pending={skipped}")
        except Exception:
            pass
    return {"published": published, "failed": failed, "pending": skipped}


def _claim_for_publish(row_id, db_path: str = DB_PATH) -> bool:
    """Move one row from 'scheduled' to 'publishing', atomically.

    run_due_posts used to call publish_now() while the row still said
    'scheduled' and only write the result afterwards. Two ways that posts
    twice to a real Instagram account: the process dies between the Graph
    call and the UPDATE, or the Graph call times out on a request Meta
    actually accepted. Either way the next tick — five minutes later — sees
    'scheduled' and publishes it again, up to MAX_ATTEMPTS.

    The UPDATE's own WHERE clause is the claim: `status='scheduled'` can only
    match once, so whoever gets rowcount 1 owns the publish.
    """
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE marketing_scheduled_posts SET status='publishing', claimed_at=datetime('now') "
            "WHERE id=? AND status='scheduled'",
            (row_id,),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def reap_stuck_publishes(older_than_minutes: int = 15, db_path: str = DB_PATH) -> int:
    """Resolve rows left in 'publishing' by a process that died mid-flight.

    We cannot know whether the post reached the platform, and guessing wrong
    in the optimistic direction puts a duplicate on the restaurant's public
    feed. So these are failed, not retried, with an error that says exactly
    what is uncertain — a missing post the owner can repost beats a double
    post they have to go and delete.
    """
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, restaurant_id, platform, topic, body, scheduled_for FROM marketing_scheduled_posts "
            "WHERE status='publishing' AND claimed_at < datetime('now', ?)",
            (f"-{int(older_than_minutes)} minutes",),
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            "UPDATE marketing_scheduled_posts SET status='failed', "
            "error='Interrupted while publishing — it may or may not have gone out. Check the platform before reposting.' "
            "WHERE status='publishing' AND claimed_at < datetime('now', ?)",
            (f"-{int(older_than_minutes)} minutes",),
        )
        conn.commit()
    finally:
        conn.close()
    for r in rows:
        try:
            _alert_failed_post(r, "Interrupted while publishing — check the platform before reposting", db_path=db_path)
        except Exception as e:
            log.warning("reap_stuck_publishes alert failed for row %s: %s", r["id"], e)
    return len(rows)


def _finish(row_id, status, *, post_id=None, error=None, attempts=None,
            db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE marketing_scheduled_posts SET status=?, error=?, "
            "attempts=COALESCE(?, attempts), post_id=COALESCE(?, post_id), "
            "posted_at=CASE WHEN ?='posted' THEN datetime('now') ELSE posted_at END "
            "WHERE id=?",
            (status, error, attempts, post_id, status, row_id),
        )
        conn.commit()
    finally:
        conn.close()
