"""What Cavnar AI has actually been doing for one restaurant.

The "AI activity feed": not alerts, and not a status page — the quiet
evidence that the platform is working between the moments an owner looks.
Every line here is read from a row some job wrote (a fetched review, a
competitor snapshot, a drafted schedule, a fired brief, a diagnosis, a
tracker still measuring) and says when. Nothing is generated to fill
space: a restaurant with no data gets an empty list, and the "working"
lines only name processes that are genuinely armed for this account
(reviews_live, a POS feed, a schedule on file). A feed that invented
activity would be the one thing this product must never do — the whole
value of the number it shows is that it was measured.

Three parts:

  working  — present tense, what is armed right now ("Watching 6
             competitors", "Next review sweep at 4pm"); the client rotates
             through them.
  entries  — past tense, timestamped, newest first ("Finished analyzing 12
             new reviews", "Prepared this morning's brief").
  memory   — what the AI is still holding: trackers in flight, follow-ups
             it was told to remember, issues it is chasing.

Cached per restaurant for ACTIVITY_TTL seconds: the feed is polled by
every open dashboard, and the answer does not change faster than the jobs
that write it.
"""
import time
from datetime import datetime, timedelta, timezone

from models import get_conn, DB_PATH

ACTIVITY_TTL = 45
_CACHE = {}


def _utc(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def _iso_z(value):
    """Normalise a stored timestamp (naive UTC, either separator) to ISO-8601
    with a Z, so the clients can compute "12 minutes ago" in the viewer's
    own clock instead of guessing which zone the string was written in."""
    if not value:
        return None
    s = str(value).replace(" ", "T")[:19]
    return s + "Z" if not s.endswith("Z") and "+" not in s else s


def _one(conn, sql, args=()):
    try:
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _row(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchone()
    except Exception:
        return None


def _plural(n, one, many=None):
    n = int(n or 0)
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _next_fetch_slot(restaurant):
    """The next review sweep, in the restaurant's clock — the fetch runs at
    8/12/4/8 Chicago, so this is that schedule translated for the owner."""
    try:
        from time_utils import restaurant_now
        from zoneinfo import ZoneInfo
        now_chi = datetime.now(ZoneInfo("America/Chicago"))
        for h in (8, 12, 16, 20):
            slot = now_chi.replace(hour=h, minute=0, second=0, microsecond=0)
            if slot > now_chi:
                break
        else:
            slot = (now_chi + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        local = slot.astimezone(restaurant_now(restaurant).tzinfo)
        return local.strftime("%-I%p").lower().replace("m", "m")
    except Exception:
        return None


def build(restaurant_id, restaurant=None, db_path=DB_PATH, denied=frozenset()):
    """The feed for one restaurant. `denied` is the set of module keys this
    viewer may not read (ask_cavnar_tools.viewer_restaurant); lines from
    those modules are left out the way the brief leaves them out."""
    from models import get_restaurant
    r = restaurant or get_restaurant(restaurant_id)
    if not r:
        return {"ok": True, "working": [], "entries": [], "memory": [], "generated_at": _iso_z(_utc())}
    now = datetime.now(timezone.utc)
    day = _utc(now - timedelta(hours=24))
    week = _utc(now - timedelta(days=7))
    month = _utc(now - timedelta(days=30))
    conn = get_conn(db_path)
    working, entries, memory = [], [], []
    try:
        # ── reviews ──────────────────────────────────────────────────────
        if getattr(r, "module_reviews", 0) and "reviews" not in denied:
            live = bool(getattr(r, "reviews_live", 0) or getattr(r, "gmb_refresh_token", None))
            n_new = _one(conn, "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                               "AND processed=1 AND fetched_at >= ?", (restaurant_id, day)) or 0
            last_fetch = _one(conn, "SELECT MAX(fetched_at) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                    "AND fetched_at >= ?", (restaurant_id, week))
            if n_new:
                entries.append({"at": _iso_z(last_fetch), "module": "reviews", "kind": "analyzed",
                                "text": f"Finished analyzing {_plural(n_new, 'new review')}."})
            drafted = _one(conn, "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                 "AND draft_response IS NOT NULL AND draft_response != '' AND fetched_at >= ?",
                           (restaurant_id, day)) or 0
            if drafted:
                entries.append({"at": _iso_z(last_fetch), "module": "reviews", "kind": "drafted",
                                "text": f"Drafted {_plural(drafted, 'reply', 'replies')} in your voice."})
            diag = _row(conn, "SELECT category, mention_count, generated_at FROM review_diagnoses "
                              "WHERE restaurant_id=? AND generated_at >= ? ORDER BY generated_at DESC LIMIT 1",
                        (restaurant_id, week))
            if diag:
                try:
                    from analyser import category_label
                    cat = category_label(diag["category"])
                except Exception:
                    cat = str(diag["category"]).replace("_", " ")
                entries.append({"at": _iso_z(diag["generated_at"]), "module": "reviews", "kind": "diagnosed",
                                "text": f"Traced the likely cause behind {_plural(diag['mention_count'], cat.lower() + ' mention')}."})
            if live:
                slot = _next_fetch_slot(r)
                working.append({"module": "reviews",
                                "text": "Watching for new reviews" + (f" · next sweep at {slot}" if slot else "")})
                n90 = _one(conn, "SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                 "AND COALESCE(NULLIF(review_date,''), fetched_at) >= ?",
                           (restaurant_id, _utc(now - timedelta(days=90)))) or 0
                if n90:
                    working.append({"module": "reviews",
                                    "text": f"Comparing this week's reviews with the last {_plural(n90, 'review')} from 90 days"})

        # ── competitors / AI visibility (Intel) ──────────────────────────
        if "intel" not in denied and "competitor" not in denied:
            comp = _row(conn, "SELECT COUNT(DISTINCT place_id) AS n, MAX(captured_at) AS at FROM competitor_snapshots "
                              "WHERE restaurant_id=? AND captured_at >= ?", (restaurant_id, month))
            if comp and comp["n"]:
                entries.append({"at": _iso_z(comp["at"]), "module": "intel", "kind": "compared",
                                "text": f"Compared {_plural(comp['n'], 'competitor')} on rating, volume and price."})
                working.append({"module": "intel", "text": f"Watching {_plural(comp['n'], 'competitor')}"})
            vis = _row(conn, "SELECT ai_score, created_at FROM ai_visibility_runs WHERE restaurant_id=? "
                             "AND ai_score IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT 1", (restaurant_id,))
            if vis and vis["created_at"] and vis["created_at"] >= month:
                entries.append({"at": _iso_z(vis["created_at"]), "module": "intel", "kind": "visibility",
                                "text": f"Checked how AI assistants describe you — visibility {vis['ai_score']} of 100."})

        # ── labor ────────────────────────────────────────────────────────
        if getattr(r, "module_labor", 0) and "labor" not in denied:
            sched = _row(conn, "SELECT week_start, generated_at, hours_scheduled, hours_budget FROM schedule_history "
                               "WHERE restaurant_id=? ORDER BY generated_at DESC LIMIT 1", (restaurant_id,))
            if sched and sched["generated_at"] and sched["generated_at"] >= month:
                entries.append({"at": _iso_z(sched["generated_at"]), "module": "labor", "kind": "scheduled",
                                "text": f"Built the schedule for the week of {sched['week_start']}."
                                        + (f" {sched['hours_scheduled']:.0f} of {sched['hours_budget']:.0f} budgeted hours."
                                           if sched["hours_scheduled"] and sched["hours_budget"] else "")})
            pos = _row(conn, "SELECT business_date, MAX(captured_hour) AS h, MAX(created_at) AS at FROM pos_intraday "
                             "WHERE restaurant_id=? AND created_at >= ?", (restaurant_id, day))
            if pos and pos["at"]:
                working.append({"module": "labor", "text": "Monitoring today's sales hour by hour"})
            hist = _one(conn, "SELECT MAX(date) FROM labor_daily_history WHERE restaurant_id=?", (restaurant_id,))
            if hist and str(hist) >= week[:10]:
                working.append({"module": "labor", "text": "Tracking labor against your target"})

        # ── food cost ────────────────────────────────────────────────────
        if getattr(r, "module_inventory", 0) and "inventory" not in denied:
            fc = _row(conn, "SELECT kind, predicted, created_at FROM forecast_log WHERE restaurant_id=? "
                            "ORDER BY created_at DESC LIMIT 1", (restaurant_id,))
            if fc and fc["created_at"] and fc["created_at"] >= week:
                what = "this week's waste" if fc["kind"] == "waste_week" else "this month's profitability"
                entries.append({"at": _iso_z(fc["created_at"]), "module": "inventory", "kind": "projected",
                                "text": f"Projected {what} from your counts and sales."})
            counted = _one(conn, "SELECT MAX(created_at) FROM inventory_history WHERE restaurant_id=?", (restaurant_id,))
            if counted and str(counted) >= month:
                working.append({"module": "inventory", "text": "Checking inventory trends against your last count"})
            loss = _one(conn, "SELECT MAX(synced_at) FROM pos_loss_daily WHERE restaurant_id=?", (restaurant_id,))
            if loss and str(loss) >= week:
                entries.append({"at": _iso_z(loss), "module": "inventory", "kind": "loss",
                                "text": "Checked comps, voids and refunds against your eight-week baseline."})

        # ── marketing ────────────────────────────────────────────────────
        if getattr(r, "module_marketing", 0) and "marketing" not in denied:
            up = _one(conn, "SELECT COUNT(*) FROM marketing_scheduled_posts WHERE restaurant_id=? "
                            "AND scheduled_for >= ? AND status NOT IN ('posted','failed','cancelled','canceled')",
                      (restaurant_id, _utc(now))) or 0
            if up:
                working.append({"module": "marketing", "text": f"Holding {_plural(up, 'post')} to publish on schedule"})
            posted = _row(conn, "SELECT COUNT(*) AS n, MAX(posted_at) AS at FROM marketing_scheduled_posts "
                                "WHERE restaurant_id=? AND status='posted' AND posted_at >= ?", (restaurant_id, week))
            if posted and posted["n"]:
                entries.append({"at": _iso_z(posted["at"]), "module": "marketing", "kind": "posted",
                                "text": f"Published {_plural(posted['n'], 'post')} on schedule this week."})

        # ── briefs, alerts, issues — the owner-facing work ───────────────
        brief = _one(conn, "SELECT MAX(fired_at) FROM alert_log WHERE restaurant_id=? AND alert_type='morning_brief' "
                           "AND fired_at >= ?", (restaurant_id, day))
        if brief:
            entries.append({"at": _iso_z(brief), "module": "home", "kind": "brief",
                            "text": "Prepared this morning's brief."})
        flagged = _row(conn, "SELECT COUNT(*) AS n, MAX(fired_at) AS at FROM alert_log WHERE restaurant_id=? "
                             "AND fired_at >= ? AND alert_type NOT IN ('morning_brief','intraday_pulse','closing_summary',"
                             "'weekly_review','monthly_review','daily_briefing','login','staff_signin')",
                       (restaurant_id, day))
        if flagged and flagged["n"]:
            entries.append({"at": _iso_z(flagged["at"]), "module": "home", "kind": "flagged",
                            "text": f"Flagged {_plural(flagged['n'], 'thing')} worth your attention."})
        try:
            import issues as _issues
            open_issues = _issues.list_issues(restaurant_id, status="open", db_path=db_path) or []
            if open_issues:
                memory.append({"module": "home", "kind": "issue",
                               "text": f"Following up on {_plural(len(open_issues), 'open issue')}."})
        except Exception:
            pass

        # ── memory: trackers in flight and what it was told to remember ─
        try:
            rows = conn.execute(
                "SELECT title, metric, started_on, evaluate_on, status FROM recommendation_outcomes "
                "WHERE restaurant_id=? AND status != 'evaluated' AND evaluate_on IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 3", (restaurant_id,)).fetchall()
        except Exception:
            rows = []
        for o in rows:
            base = (o["metric"] or "").split(":", 1)[0]
            if base in ("food_cost_pct", "weekly_waste") and "inventory" in denied:
                continue
            if base in ("labor_pct", "sales") and "labor" in denied:
                continue
            try:
                started = datetime.fromisoformat(str(o["started_on"])[:10]).date()
                ends = datetime.fromisoformat(str(o["evaluate_on"])[:10]).date()
                today = now.date()
                total = max(1, (ends - started).days)
                dayn = min(total, max(1, (today - started).days + 1))
                memory.append({"module": "outcomes", "kind": "tracker",
                               "text": f"Still measuring “{o['title']}” — day {dayn} of {total}."})
            except Exception:
                memory.append({"module": "outcomes", "kind": "tracker",
                               "text": f"Still measuring “{o['title']}”."})
        try:
            from models import get_ask_memory
            for m in (get_ask_memory(restaurant_id, db_path=db_path) or [])[:2]:
                if (m.get("kind") or "") in ("followup", "goal"):
                    memory.append({"module": "ask", "kind": m["kind"],
                                   "text": f"Remembering: {m['fact']}"})
        except Exception:
            pass

        # ── the signal count, the same way Home counts them ──────────────
        try:
            import metrics
            from monthly_review import HEADLINE_METRICS
            keys = [k for k in HEADLINE_METRICS
                    if not (k == "food_cost_pct" and "inventory" in denied)
                    and not (k in ("labor_pct", "sales") and "labor" in denied)]
            live_keys = [k for k in keys if metrics.trailing(restaurant_id, k, db_path=db_path)["value"] is not None]
            if live_keys:
                working.append({"module": "home",
                                "text": f"Monitoring {_plural(len(live_keys), 'operational signal')}"})
        except Exception:
            pass
    finally:
        conn.close()

    entries.sort(key=lambda e: e.get("at") or "", reverse=True)
    # The last sweep, platform-wide: real and recent, so an owner sees the
    # clock the product runs on.
    last_run = None
    try:
        c2 = get_conn(db_path)
        try:
            row = c2.execute("SELECT job, finished_at FROM job_runs WHERE ok=1 AND job IN "
                             "('review_fetch','pos_sync','competitor_analysis','ai_visibility') "
                             "ORDER BY id DESC LIMIT 1").fetchone()
            if row and row["finished_at"]:
                last_run = {"job": row["job"], "at": _iso_z(row["finished_at"])}
        finally:
            c2.close()
    except Exception:
        pass
    return {"ok": True, "working": working, "entries": entries[:12], "memory": memory[:5],
            "last_run": last_run, "generated_at": _iso_z(_utc(now))}


def feed(restaurant_id, restaurant=None, db_path=DB_PATH, denied=frozenset()):
    key = (restaurant_id, frozenset(denied), db_path)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ACTIVITY_TTL:
        return hit[1]
    out = build(restaurant_id, restaurant=restaurant, db_path=db_path, denied=denied)
    _CACHE[key] = (time.time(), out)
    return out
