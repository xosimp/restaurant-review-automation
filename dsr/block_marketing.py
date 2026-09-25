"""
dsr.block_marketing — what went out on one night, what it did where that
was measured, and what's queued next.

Sources (no model call, and nothing estimated):
  posts published   marketing_content_log rows with a platform post id whose
                    publish time (posted_at, else created_at — UTC) falls in
                    the business date. Only what went out through Cavnar;
                    a post made in the Instagram app is not seen.
  engagement        the reach / impressions / likes / comments / shares Meta
                    returned for those posts (social_routes' insights
                    refresh). The columns default to 0, so a post counts as
                    measured only when something was seen (the
                    marketing_signals.performance_window rule): no reach is
                    "not measured", never "nobody engaged".
  failed posts      scheduled posts due in the window that failed
                    (marketing_publish)
  campaigns         guest texts (guest_marketing.campaign_history) and email
                    newsletters (guest_newsletters) sent in the window, with
                    their results only where attribution exists: link clicks
                    when the text carried a tracked link, visits matched once
                    run_campaign_attribution has read the days after
                    (attribution_through). Sales lift is never estimated.
  earlier results   campaigns from the attribution window before the night
                    whose measured results moved
  upcoming          scheduled posts in the next 7 days
                    (marketing_publish.list_scheduled), drafts waiting for
                    approval (marketing_drafts.list_drafts), a pending
                    win-back text (guest_campaign_drafts — read here, never
                    created: guest_marketing.winback_suggestion writes), and
                    the holidays in the next 7 days (marketing.get_upcoming_holidays)

Statuses: not_connected when Marketing is off for the location; unavailable
when nothing could be read; ready otherwise.
"""
import re
from datetime import datetime, timedelta

import dsr
from dsr import common

UPCOMING_DAYS = 7


def _rows(ctx, sql, args):
    from dsr.store import get_conn
    conn = get_conn(ctx.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _local_label(ctx, utc_stamp):
    dt = common.parse_utc(utc_stamp)
    return common.time_label(common.to_local(dt, ctx.restaurant)) if dt else None


def _posts(ctx, u0, u1):
    rows = _rows(ctx, "SELECT id, topic, post_platform, COALESCE(posted_at, created_at) AS at, "
                      "COALESCE(reach,0) + COALESCE(impressions,0) AS seen, "
                      "COALESCE(likes,0) + COALESCE(comments,0) + COALESCE(shares,0) AS engaged "
                      "FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL "
                      "AND TRIM(post_id) != '' AND datetime(COALESCE(posted_at, created_at)) >= ? "
                      "AND datetime(COALESCE(posted_at, created_at)) < ? ORDER BY at",
                 (ctx.restaurant_id, u0, u1))
    measured = [p for p in rows if (p["seen"] or 0) > 0]
    seen = sum(p["seen"] for p in measured)
    engaged = sum(p["engaged"] for p in measured)
    return {
        "count": len(rows),
        "reach": seen if measured else None,
        "engagement": engaged if measured else None,
        "engagement_rate": round(engaged / seen * 100, 1) if seen else None,
        "items": [{"topic": p["topic"], "platform": p["post_platform"], "at": _local_label(ctx, p["at"]),
                   "reach": p["seen"] if (p["seen"] or 0) > 0 else None,
                   "engagement": p["engaged"] if (p["seen"] or 0) > 0 else None} for p in rows[:10]],
        "measured": len(measured),
        "basis": "posts published through Cavnar AI; reach and engagement as Meta last reported them",
    }


def _failed(ctx, start, end):
    fmt = "%Y-%m-%dT%H:%M:%S"
    rows = _rows(ctx, "SELECT platform, topic, scheduled_for, error FROM marketing_scheduled_posts "
                      "WHERE restaurant_id=? AND status='failed' AND scheduled_for >= ? AND scheduled_for < ?",
                 (ctx.restaurant_id, start.strftime(fmt), end.strftime(fmt)))
    return [{"platform": r["platform"], "topic": r["topic"], "error": r["error"]} for r in rows]


def _campaigns(ctx, u0, u1):
    import guest_marketing
    from time_utils import mdy
    hist = guest_marketing.campaign_history(ctx.restaurant_id, limit=40, db_path=ctx.db_path)
    lo = (datetime.strptime(u0, "%Y-%m-%d %H:%M:%S")
          - timedelta(days=guest_marketing.ATTRIBUTION_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")

    def when(c):
        dt = common.parse_utc(c.get("created_at"))
        return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""

    def result(c):
        # Measured or absent: clicks only when the text carried a tracked
        # link; visits only once attribution has read a day after the send.
        return {"message": (c.get("message") or "")[:160], "segment": c.get("segment_label") or c.get("segment"),
                "sent": c.get("sent_count"), "failed": c.get("failed_count"),
                "clicks": c.get("clicks") if c.get("link_token") else None,
                "visits_matched": c.get("visits_matched") if c.get("attribution_through") else None,
                "attributed_through": mdy(c["attribution_through"]) if c.get("attribution_through") else None,
                "sent_at": _local_label(ctx, c.get("created_at"))}
    today = [c for c in hist if u0 <= when(c) < u1]
    earlier = [c for c in hist if lo <= when(c) < u0
               and (c.get("attribution_through") or (c.get("link_token") and c.get("clicks")))]
    news = _rows(ctx, "SELECT subject, total, created_at FROM guest_newsletters WHERE restaurant_id=? "
                      "AND datetime(created_at) >= ? AND datetime(created_at) < ?", (ctx.restaurant_id, u0, u1))
    linked = [c for c in today if c.get("link_token")]
    attributed = [c for c in today if c.get("attribution_through")]
    return {
        "texts": [result(c) for c in today],
        "newsletters": [{"subject": n["subject"], "sent": n["total"], "sent_at": _local_label(ctx, n["created_at"])}
                        for n in news],
        "earlier_results": [result(c) for c in earlier[:5]],
        "clicks": sum(int(c.get("clicks") or 0) for c in linked) if linked else None,
        "visits": sum(int(c.get("visits_matched") or 0) for c in attributed) if attributed else None,
        "texts_sent": sum(int(c.get("sent_count") or 0) for c in today),
        "basis": ("clicks on a tracked link, and guests the POS matched on a check after the text — "
                  "partial by nature, and never a sales estimate"),
    }


_HOLIDAY = re.compile(r"^(.*) \(([A-Z][a-z]{2}) (\d{2})\)$")


def _holidays(ctx):
    from marketing import get_upcoming_holidays
    from time_utils import mdy
    first = ctx.business_date + timedelta(days=1)
    raw = get_upcoming_holidays(datetime.combine(first, datetime.min.time())) or ""
    out = []
    for h in raw.split(", "):
        m = _HOLIDAY.match(h.strip())
        if not m:
            continue
        for year in (first.year, first.year + 1):
            try:
                d = datetime.strptime(f"{m.group(2)} {m.group(3)} {year}", "%b %d %Y").date()
            except ValueError:
                continue
            if first <= d < first + timedelta(days=UPCOMING_DAYS):
                out.append({"name": m.group(1).split(" — ")[0], "date": mdy(d), "iso": d.isoformat()})
                break
    return sorted(out, key=lambda x: x["iso"])


def _upcoming(ctx, end_local, gaps):
    import marketing_drafts
    import marketing_publish
    fmt = "%Y-%m-%dT%H:%M:%S"
    horizon = end_local + timedelta(days=UPCOMING_DAYS)
    sched = common.guard(ctx, "marketing", "scheduled", lambda: [
        s for s in marketing_publish.list_scheduled(ctx.restaurant_id, include_done=False, limit=50,
                                                    db_path=ctx.db_path)
        if end_local.strftime(fmt) <= str(s.get("scheduled_for") or "")[:19] < horizon.strftime(fmt)], gaps) or []
    drafts = common.guard(ctx, "marketing", "drafts", lambda: [
        d for d in marketing_drafts.list_drafts(ctx.restaurant_id, limit=40, db_path=ctx.db_path)
        if (d.get("status") or "draft") == "draft"], gaps) or []
    winback = common.guard(ctx, "marketing", "winback", lambda: _rows(
        ctx, "SELECT segment, segment_size, created_at FROM guest_campaign_drafts WHERE restaurant_id=? "
             "AND kind='winback' AND status='pending' ORDER BY id DESC LIMIT 1", (ctx.restaurant_id,)), gaps) or []
    holidays = common.guard(ctx, "marketing", "holidays", lambda: _holidays(ctx), gaps) or []

    def at(s):
        dt = common.local_stamp(s.get("scheduled_for"), ctx.restaurant)
        return common.time_label(dt) if dt else None
    return {
        "scheduled": [{"platform": s.get("platform"), "topic": s.get("topic"), "at": at(s)} for s in sched[:10]],
        "scheduled_count": len(sched),
        "drafts": [{"topic": d.get("topic"), "type": d.get("content_type"),
                    "by": d.get("created_by_name")} for d in drafts[:10]],
        "drafts_count": len(drafts),
        "winback": ({"segment": winback[0]["segment"], "guests": winback[0]["segment_size"],
                     "note": "a win-back text drafted and waiting for the owner"} if winback else None),
        "holidays": [{"name": h["name"], "date": h["date"]} for h in holidays],
        "horizon_days": UPCOMING_DAYS,
    }


def collect(ctx):
    r = ctx.restaurant
    if not getattr(r, "module_marketing", 1):
        return dsr.block(dsr.NOT_CONNECTED, source="cavnar", block_name="marketing",
                         reason="Marketing isn't switched on for this location")
    gaps = []
    start, end = common.local_bounds(r, ctx.business_date)
    u0, u1 = common.utc_bounds(r, ctx.business_date)
    posts = common.guard(ctx, "marketing", "posts", lambda: _posts(ctx, u0, u1), gaps)
    failed = common.guard(ctx, "marketing", "failed_posts", lambda: _failed(ctx, start, end), gaps)
    camps = common.guard(ctx, "marketing", "campaigns", lambda: _campaigns(ctx, u0, u1), gaps)
    upcoming = _upcoming(ctx, end, gaps)
    if posts is None and camps is None:
        return dsr.block(dsr.UNAVAILABLE, source="cavnar", block_name="marketing",
                         detail={"unavailable_parts": gaps})

    metrics = {
        "posts_published": posts["count"] if posts else None,
        "posts_failed": len(failed) if failed is not None else None,
        "reach": posts["reach"] if posts else None,
        "engagement": posts["engagement"] if posts else None,
        "engagement_rate": posts["engagement_rate"] if posts else None,
        "campaigns_sent": len(camps["texts"]) + len(camps["newsletters"]) if camps else None,
        "texts_sent": camps["texts_sent"] if camps else None,
        "campaign_clicks": camps["clicks"] if camps else None,
        "campaign_visits": camps["visits"] if camps else None,
        "scheduled_next_7d": upcoming["scheduled_count"] if "scheduled" not in gaps else None,
        "drafts_awaiting": upcoming["drafts_count"] if "drafts" not in gaps else None,
    }
    detail = {"posts": posts, "failed_posts": failed, "campaigns": camps, "upcoming": upcoming,
              "unavailable_parts": gaps}
    return dsr.block(dsr.READY, source="cavnar", metrics=metrics, detail=detail)
