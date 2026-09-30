"""history_rollups — what outlives a prune (memory audit 9/29/26: "rollups",
"review_retention", "seasonal_food").

Raw ledgers are kept for a window (ops' one retention registry) and then
deleted; the owner's review-retention choice soft-deletes reviews; the
inventory history is dropped past 395 days. Each of those used to take its
history with it: "alerts sent since you started" fell in month 14, a
year-old newsletter read a measured 0 opens, "average rating by month"
shrank, and a seasonal food-cost re-check silently lost last year's counts.

Each summary here is written BEFORE its raw rows go, holds no guest text,
and is kept forever (none is in the retention registry):

  alert_monthly           restaurant × month × alert type: fired, dollars,
                          opened (ops rollup for alert_log)
  engagement_monthly      login × restaurant × month: sign-ins, active days,
                          notification opens by type (the rollup for
                          login_history and notification_opens)
  guest_newsletters.*     opens / clicks / tracked stamped on the newsletter
                          row 30 days after it went (the rollup for email_log)
  review_monthly_stats    the reviews a retention purge soft-deleted, per
                          month: count, rating sum, responded, drafted,
                          posted, the edit mix — written in the purge's own
                          transaction (models.purge_expired_reviews)
  inventory_weekly_summary  one row per restaurant × ISO week of the counted
                          snapshot that stood for it: inventory value, waste
                          $ and its top items, 7-day COGS and food cost %
  ingredient_cost_monthly the unit cost of each ingredient per month

A month (or week) is rolled up only while its raw rows are all still there,
and never rewritten after the prune has started on it, so a summary can
never be recomputed from a half-deleted month. Lifetime readers read the
summary for summarised months and the raw rows for the rest.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

import models as _models      # constants at import; get_conn looked up at call time

log = logging.getLogger(__name__)

NEWSLETTER_RESULTS_DAYS = 30

_TABLES = (
    """CREATE TABLE IF NOT EXISTS alert_monthly (
        restaurant_id  INTEGER NOT NULL,
        month          TEXT    NOT NULL,
        alert_type     TEXT    NOT NULL,
        fired          INTEGER NOT NULL DEFAULT 0,
        dollars        REAL,
        opened         INTEGER NOT NULL DEFAULT 0,
        rolled_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, month, alert_type)
    )""",
    """CREATE TABLE IF NOT EXISTS engagement_monthly (
        user_id        INTEGER NOT NULL,
        restaurant_id  INTEGER NOT NULL,
        month          TEXT    NOT NULL,
        logins         INTEGER,
        login_days     INTEGER,
        opens          INTEGER,
        opens_by_type  TEXT,
        logins_rolled_at TEXT,
        opens_rolled_at  TEXT,
        PRIMARY KEY (user_id, restaurant_id, month)
    )""",
    """CREATE TABLE IF NOT EXISTS review_monthly_stats (
        restaurant_id  INTEGER NOT NULL,
        month          TEXT    NOT NULL,
        reviews        INTEGER NOT NULL DEFAULT 0,
        rating_sum     REAL    NOT NULL DEFAULT 0,
        rated          INTEGER NOT NULL DEFAULT 0,
        responded      INTEGER NOT NULL DEFAULT 0,
        drafted        INTEGER NOT NULL DEFAULT 0,
        posted         INTEGER NOT NULL DEFAULT 0,
        edits_json     TEXT,
        purged_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, month)
    )""",
    """CREATE TABLE IF NOT EXISTS inventory_weekly_summary (
        restaurant_id  INTEGER NOT NULL,
        week           TEXT    NOT NULL,
        week_end       TEXT    NOT NULL,
        inv_value      REAL,
        waste_value    REAL,
        waste_json     TEXT,
        cogs           REAL,
        food_cost_pct  REAL,
        source         TEXT,
        summarised_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, week)
    )""",
    """CREATE TABLE IF NOT EXISTS ingredient_cost_monthly (
        restaurant_id  INTEGER NOT NULL,
        month          TEXT    NOT NULL,
        ingredient_key TEXT    NOT NULL,
        ingredient     TEXT,
        unit           TEXT,
        supplier       TEXT,
        unit_cost_avg  REAL,
        unit_cost_last REAL,
        n              INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (restaurant_id, month, ingredient_key)
    )""",
)


def init_history_rollups(db_path=None):
    """The summary tables, at boot (models.init_db)."""
    conn = _models.get_conn(db_path) if db_path else _models.get_conn()
    try:
        for sql in _TABLES:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _conn(db_path=None):
    return _models.get_conn(db_path) if db_path else _models.get_conn()


def _utcnow(now=None):
    return now or datetime.now(timezone.utc).replace(tzinfo=None)


def _month_start(ym: str) -> date:
    return date(int(ym[:4]), int(ym[5:7]), 1)


def _next_month(ym: str) -> str:
    d = _month_start(ym)
    return (date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)).strftime("%Y-%m")


def _rollable_months(conn, table, column, days, now=None):
    """Complete months of `table` whose rows are ALL still there: the month
    has ended and its first day is inside the retention window. None when
    the table's prune is off or refused (nothing is deleted, so nothing
    needs rolling — but readers still get the raw rows)."""
    now = _utcnow(now)
    this_month = now.strftime("%Y-%m")
    oldest_whole = (now - timedelta(days=int(days))).date() if days else None
    try:
        months = [r[0] for r in conn.execute(
            f"SELECT DISTINCT substr({column}, 1, 7) FROM {table} WHERE {column} IS NOT NULL").fetchall() if r[0]]
    except Exception as e:
        if "no such table" in str(e).lower():
            return []          # nothing written yet on this database, so nothing to summarise
        raise
    out = []
    for m in months:
        if len(m) != 7 or m >= this_month:
            continue
        try:
            start = _month_start(m)
        except ValueError:
            continue
        if oldest_whole is not None and start < oldest_whole:
            continue          # the prune has started on it: its summary stands as written
        out.append(m)
    return sorted(out)


def _window(table):
    import ops
    return ops.retention_days(table)


# ── alerts ────────────────────────────────────────────────────────────────

def roll_alerts(db_path=None, now=None) -> dict:
    """alert_monthly for every complete month alert_log still holds whole:
    alerts fired and their dollars per type, and the opens recorded for that
    type that month. Idempotent (a month is recomputed whole while it can
    be). The rollup ops.prune_ledgers runs before alert_log is pruned."""
    conn = _conn(db_path)
    written = 0
    try:
        months = _rollable_months(conn, "alert_log", "fired_at", _window("alert_log") or 0, now)
        for m in months:
            nxt = _next_month(m)
            rows = conn.execute(
                "SELECT restaurant_id, alert_type, COUNT(*) AS fired, SUM(value) AS dollars FROM alert_log "
                "WHERE fired_at >= ? AND fired_at < ? GROUP BY restaurant_id, alert_type",
                (m + "-01", nxt + "-01")).fetchall()
            opens = {}
            try:
                for o in conn.execute(
                        "SELECT restaurant_id, alert_type, COUNT(*) AS n FROM notification_opens "
                        "WHERE opened_at >= ? AND opened_at < ? GROUP BY restaurant_id, alert_type",
                        (m + "-01", nxt + "-01")).fetchall():
                    opens[(o["restaurant_id"], o["alert_type"])] = int(o["n"] or 0)
            except Exception:
                pass
            conn.execute("DELETE FROM alert_monthly WHERE month=?", (m,))
            for r in rows:
                conn.execute("INSERT INTO alert_monthly (restaurant_id, month, alert_type, fired, dollars, opened) "
                             "VALUES (?,?,?,?,?,?)",
                             (r["restaurant_id"], m, r["alert_type"], int(r["fired"] or 0), r["dollars"],
                              opens.get((r["restaurant_id"], r["alert_type"]), 0)))
                written += 1
            conn.commit()
    finally:
        conn.close()
    return {"months": len(months), "rows": written}


def alerts_lifetime(restaurant_id, conn=None, db_path=None) -> dict:
    """{"alerts": n, "months": {YYYY-MM, ...}} since sign-up: the monthly
    summary for summarised months and alert_log for the rest — a lifetime
    count that never falls when alert_log is pruned."""
    own = conn is None
    c = _conn(db_path) if own else conn
    try:
        rolled = {r[0]: int(r[1] or 0) for r in c.execute(
            "SELECT month, SUM(fired) FROM alert_monthly WHERE restaurant_id=? GROUP BY month",
            (restaurant_id,)).fetchall()}
        raw = {r[0]: int(r[1] or 0) for r in c.execute(
            "SELECT substr(fired_at, 1, 7) AS m, COUNT(*) FROM alert_log WHERE restaurant_id=? GROUP BY m",
            (restaurant_id,)).fetchall() if r[0]}
    finally:
        if own:
            c.close()
    total = sum(rolled.values()) + sum(n for m, n in raw.items() if m not in rolled)
    months = {m for m, n in rolled.items() if n} | {m for m, n in raw.items() if n}
    return {"alerts": total, "months": months}


# ── engagement: sign-ins and notification opens, per login ────────────────

def roll_engagement(db_path=None, now=None) -> dict:
    """engagement_monthly: per login, restaurant and complete month, the
    sign-ins and distinct sign-in days (from login_history, while its
    month is whole) and the notification opens by type (from
    notification_opens, likewise). Each half is written on its own clock —
    login_history is kept 90 days, the opens a year. The rollup
    ops.prune_ledgers runs before either is pruned."""
    conn = _conn(db_path)
    out = {"login_months": 0, "open_months": 0}
    try:
        for m in _rollable_months(conn, "login_history", "created_at", _window("login_history") or 0, now):
            nxt = _next_month(m)
            for r in conn.execute(
                    "SELECT user_id, COALESCE(restaurant_id, 0) AS rid, COUNT(*) AS n, "
                    "COUNT(DISTINCT substr(created_at, 1, 10)) AS days FROM login_history "
                    "WHERE created_at >= ? AND created_at < ? AND user_id IS NOT NULL GROUP BY user_id, rid",
                    (m + "-01", nxt + "-01")).fetchall():
                conn.execute(
                    "INSERT INTO engagement_monthly (user_id, restaurant_id, month, logins, login_days, "
                    "logins_rolled_at) VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(user_id, restaurant_id, month) "
                    "DO UPDATE SET logins=excluded.logins, login_days=excluded.login_days, "
                    "logins_rolled_at=excluded.logins_rolled_at", (r["user_id"], r["rid"], m, r["n"], r["days"]))
            out["login_months"] += 1
            conn.commit()
        for m in _rollable_months(conn, "notification_opens", "opened_at",
                                  _window("notification_opens") or 0, now):
            nxt = _next_month(m)
            per = {}
            for r in conn.execute(
                    "SELECT COALESCE(user_id, 0) AS uid, restaurant_id, alert_type, COUNT(*) AS n "
                    "FROM notification_opens WHERE opened_at >= ? AND opened_at < ? "
                    "GROUP BY uid, restaurant_id, alert_type", (m + "-01", nxt + "-01")).fetchall():
                per.setdefault((r["uid"], r["restaurant_id"]), {})[r["alert_type"]] = int(r["n"] or 0)
            for (uid, rid), by_type in per.items():
                conn.execute(
                    "INSERT INTO engagement_monthly (user_id, restaurant_id, month, opens, opens_by_type, "
                    "opens_rolled_at) VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(user_id, restaurant_id, month) "
                    "DO UPDATE SET opens=excluded.opens, opens_by_type=excluded.opens_by_type, "
                    "opens_rolled_at=excluded.opens_rolled_at",
                    (uid, rid, m, sum(by_type.values()), json.dumps(by_type, sort_keys=True)))
            out["open_months"] += 1
            conn.commit()
    finally:
        conn.close()
    return out


# ── newsletters: results stamped on the row ───────────────────────────────

def stamp_newsletter_results(db_path=None, now=None) -> dict:
    """Stamp each newsletter's recorded opens and clicks on its own row
    NEWSLETTER_RESULTS_DAYS after it went (guest_newsletters.opens_recorded,
    clicks_recorded, tracked_recorded, results_stamped_at), from email_log
    while its rows are there. newsletter_history reads the stamp once it is
    set, so email_log (kept 365 days) can age out without a year-old
    newsletter reading a measured 0 opens. The rollup ops.prune_ledgers runs
    before email_log is pruned."""
    now = _utcnow(now)
    cutoff = (now - timedelta(days=NEWSLETTER_RESULTS_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn(db_path)
    stamped = 0
    try:
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM guest_newsletters WHERE results_stamped_at IS NULL AND created_at <= ?",
                (cutoff,)).fetchall()]
        except Exception as e:
            if "no such" in str(e).lower():
                return {"stamped": 0}
            raise
        try:
            import guest_email
            tracking = guest_email.opens_tracked(db_path) if ids else False
        except Exception:
            tracking = False
        for nid in ids:
            r = conn.execute(
                "SELECT SUM(CASE WHEN r.message_id IS NOT NULL THEN 1 ELSE 0 END) AS tracked, "
                "SUM(CASE WHEN e.opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
                "SUM(CASE WHEN e.clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked "
                "FROM guest_newsletter_recipients r "
                "LEFT JOIN email_log e ON r.message_id IS NOT NULL AND e.message_id = r.message_id "
                "WHERE r.newsletter_id=?", (nid,)).fetchone()
            # With open tracking off on the Resend side, the opens are
            # unknown — stamped NULL, never a measured zero.
            conn.execute("UPDATE guest_newsletters SET opens_recorded=?, clicks_recorded=?, tracked_recorded=?, "
                         "results_stamped_at=datetime('now') WHERE id=? AND results_stamped_at IS NULL",
                         (int(r["opened"] or 0) if tracking else None, int(r["clicked"] or 0) if tracking else None,
                          int(r["tracked"] or 0), nid))
            stamped += 1
        conn.commit()
    finally:
        conn.close()
    return {"stamped": stamped}


# ── reviews: what the owner's retention choice removed ─────────────────────

_REVIEW_AXIS = "COALESCE(NULLIF(review_date,''), fetched_at)"


def purge_reviews(conn, restaurant_id, months) -> int:
    """Soft-delete the restaurant's reviews older than `months` × 30 days,
    and — in the same transaction, before it — add what they were to
    review_monthly_stats: per month of the review, the count, the rating
    sum, how many were responded to, drafted and posted, and the edit mix.
    No guest text. The caller commits (models.purge_expired_reviews).
    Returns rows soft-deleted."""
    where = (f"restaurant_id=? AND deleted_at IS NULL "
             f"AND {_REVIEW_AXIS} < datetime('now', '-{int(months) * 30} days')")
    rows = conn.execute(
        f"SELECT substr({_REVIEW_AXIS}, 1, 7) AS m, COUNT(*) AS n, SUM(COALESCE(rating, 0)) AS rsum, "
        f"SUM(CASE WHEN rating IS NOT NULL THEN 1 ELSE 0 END) AS rated, "
        f"SUM(CASE WHEN response_status IN ('posted','approved') THEN 1 ELSE 0 END) AS responded, "
        f"SUM(CASE WHEN draft_response IS NOT NULL THEN 1 ELSE 0 END) AS drafted, "
        f"SUM(CASE WHEN response_status='posted' THEN 1 ELSE 0 END) AS posted "
        f"FROM reviews WHERE {where} GROUP BY m", (restaurant_id,)).fetchall()
    edits = {}
    try:
        for e in conn.execute(f"SELECT substr({_REVIEW_AXIS}, 1, 7) AS m, edit_category, COUNT(*) AS n "
                              f"FROM reviews WHERE {where} AND edit_category IS NOT NULL "
                              f"GROUP BY m, edit_category", (restaurant_id,)).fetchall():
            edits.setdefault(e["m"], {})[e["edit_category"]] = int(e["n"] or 0)
    except Exception:
        pass
    for r in rows:
        m = r["m"] or "unknown"
        prev = conn.execute("SELECT edits_json FROM review_monthly_stats WHERE restaurant_id=? AND month=?",
                            (restaurant_id, m)).fetchone()
        mix = {}
        if prev and prev["edits_json"]:
            try:
                mix = json.loads(prev["edits_json"]) or {}
            except (TypeError, ValueError):
                mix = {}
        for k, n in (edits.get(r["m"]) or {}).items():
            mix[k] = int(mix.get(k, 0)) + n
        conn.execute(
            "INSERT INTO review_monthly_stats (restaurant_id, month, reviews, rating_sum, rated, responded, drafted, "
            "posted, edits_json) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, month) DO UPDATE SET "
            "reviews=reviews+excluded.reviews, rating_sum=rating_sum+excluded.rating_sum, rated=rated+excluded.rated, "
            "responded=responded+excluded.responded, drafted=drafted+excluded.drafted, posted=posted+excluded.posted, "
            "edits_json=excluded.edits_json, purged_at=datetime('now')",
            (restaurant_id, m, int(r["n"] or 0), float(r["rsum"] or 0), int(r["rated"] or 0),
             int(r["responded"] or 0), int(r["drafted"] or 0), int(r["posted"] or 0),
             json.dumps(mix, sort_keys=True) if mix else None))
    cur = conn.execute(f"UPDATE reviews SET deleted_at = datetime('now') WHERE {where}", (restaurant_id,))
    return cur.rowcount or 0


# The words on a review row that are the guest's, or a model's or the
# owner's about the guest: blanked once a removed review is past its undo
# window (erase_removed_reviews) and in every off-site copy for any removed
# review (offsite_backup.SCRUB_ROWS). The rating, dates, status and the
# platform key stay — the key is what stops a re-fetch re-adding the review.
REVIEW_GUEST_TEXT = ("author", "text", "summary", "draft_response", "original_draft", "entities",
                     "specific_complaint", "edit_signals", "draft_review_reason")
REVIEW_REMOVED_SQL = "deleted_at IS NOT NULL"


def _review_text_columns(conn) -> list:
    have = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    return [c for c in REVIEW_GUEST_TEXT if c in have]


def review_erase_assignments(conn) -> str:
    """The SET clause that blanks a review's guest text: '' for a NOT NULL
    column (text), NULL for the rest."""
    notnull = {r[1] for r in conn.execute("PRAGMA table_info(reviews)") if r[3]}
    return ", ".join(f"{c}={chr(39) * 2 if c in notnull else 'NULL'}" for c in _review_text_columns(conn))


def erase_removed_reviews(conn, days, deadline=None, max_rows=200000, chunk=5000) -> int:
    """Erase the words of reviews removed more than `days` ago (memory
    re-audit 9/29/26, FORGET-1). "Removed" was only hidden: the guest's name,
    text and every drafted reply stayed in the database and in every backup
    for good. The soft delete is the undo window; past it the guest text
    columns (REVIEW_GUEST_TEXT) are blanked and `erased_at` stamped. The row
    stays so the platform key keeps a re-fetch from re-adding it, and its
    month is already in review_monthly_stats (purge_reviews). Chunked, a
    commit per chunk, at most `max_rows` a pass. Returns rows erased."""
    import time as _time
    sets = review_erase_assignments(conn)
    total = 0
    while total < max_rows:
        if deadline is not None and _time.monotonic() > deadline:
            break
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM reviews WHERE {REVIEW_REMOVED_SQL} AND erased_at IS NULL "
            f"AND deleted_at < datetime('now', ?) LIMIT ?",
            (f"-{int(days)} days", min(chunk, max_rows - total))).fetchall()]
        if not ids:
            break
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE reviews SET {sets}, erased_at=datetime('now') WHERE id IN ({marks})", ids)
        conn.commit()
        total += len(ids)
    return total


def purged_review_totals(restaurant_id, conn=None, db_path=None) -> dict:
    """{reviews, drafted, posted} the retention purge removed — added to the
    live counts by the lifetime ledger, so "replies drafted since you
    started" does not shrink when the owner keeps six months."""
    own = conn is None
    c = _conn(db_path) if own else conn
    try:
        r = c.execute("SELECT SUM(reviews), SUM(drafted), SUM(posted) FROM review_monthly_stats WHERE restaurant_id=?",
                      (restaurant_id,)).fetchone()
    except Exception:
        r = None
    finally:
        if own:
            c.close()
    return {"reviews": int((r[0] if r else 0) or 0), "drafted": int((r[1] if r else 0) or 0),
            "posted": int((r[2] if r else 0) or 0)}


def review_months(restaurant_id, db_path=None) -> list:
    """Every month's review record, oldest first — the live reviews and the
    purged ones together, no guest text: [{month, reviews, avg_rating,
    response_rate, drafted, posted, purged}]. Average rating by month stays
    answerable after the owner's retention purge."""
    conn = _conn(db_path)
    try:
        live = {r["m"]: dict(r) for r in conn.execute(
            f"SELECT substr({_REVIEW_AXIS}, 1, 7) AS m, COUNT(*) AS reviews, SUM(COALESCE(rating,0)) AS rsum, "
            f"SUM(CASE WHEN rating IS NOT NULL THEN 1 ELSE 0 END) AS rated, "
            f"SUM(CASE WHEN response_status IN ('posted','approved') THEN 1 ELSE 0 END) AS responded, "
            f"SUM(CASE WHEN draft_response IS NOT NULL THEN 1 ELSE 0 END) AS drafted, "
            f"SUM(CASE WHEN response_status='posted' THEN 1 ELSE 0 END) AS posted "
            f"FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL GROUP BY m", (restaurant_id,)).fetchall()
            if r["m"]}
        gone = {r["month"]: dict(r) for r in conn.execute(
            "SELECT * FROM review_monthly_stats WHERE restaurant_id=?", (restaurant_id,)).fetchall()}
    finally:
        conn.close()
    out = []
    for m in sorted(set(live) | set(gone)):
        a, b = live.get(m) or {}, gone.get(m) or {}
        n = int(a.get("reviews") or 0) + int(b.get("reviews") or 0)
        rated = int(a.get("rated") or 0) + int(b.get("rated") or 0)
        rsum = float(a.get("rsum") or 0) + float(b.get("rating_sum") or 0)
        responded = int(a.get("responded") or 0) + int(b.get("responded") or 0)
        out.append({"month": m, "reviews": n, "avg_rating": round(rsum / rated, 2) if rated else None,
                    "response_rate": round(responded / n * 100, 1) if n else None,
                    "drafted": int(a.get("drafted") or 0) + int(b.get("drafted") or 0),
                    "posted": int(a.get("posted") or 0) + int(b.get("posted") or 0),
                    "purged": int(b.get("reviews") or 0)})
    return out


# ── inventory: the week's summary and the month's ingredient costs ────────

def _iso_monday(day: str) -> str:
    d = date.fromisoformat(str(day)[:10])
    return (d - timedelta(days=d.weekday())).isoformat()


def summarise_inventory(conn, before_day, deadline=None) -> int:
    """Before ops deletes inventory_history rows dated before `before_day`
    (ISO): one inventory_weekly_summary row per restaurant and ISO week (the
    week's newest snapshot — the one that stood for it), and the month's
    unit cost per ingredient into ingredient_cost_monthly. A week already
    summarised is left as it was (INSERT OR IGNORE: its counts were whole
    when it was written). Commits. Returns summary rows written."""
    import time as _time
    rows = conn.execute(
        "SELECT id, restaurant_id, week_end, waste_json, items_json, inv_value, source FROM inventory_history "
        "WHERE week_end < ? AND week_end IS NOT NULL ORDER BY restaurant_id, week_end, id", (before_day,)).fetchall()
    newest = {}
    for r in rows:
        try:
            wk = _iso_monday(r["week_end"])
        except ValueError:
            continue
        newest[(r["restaurant_id"], wk)] = r       # ascending: the last one wins
    written = 0
    for (rid, wk), r in sorted(newest.items()):
        if deadline is not None and _time.monotonic() > deadline:
            break
        if conn.execute("SELECT 1 FROM inventory_weekly_summary WHERE restaurant_id=? AND week=?",
                        (rid, wk)).fetchone():
            continue
        try:
            waste = json.loads(r["waste_json"] or "{}") or {}
        except (TypeError, ValueError):
            waste = {}
        cogs_v, pct = _week_food_cost(rid, r["week_end"])
        conn.execute(
            "INSERT OR IGNORE INTO inventory_weekly_summary (restaurant_id, week, week_end, inv_value, waste_value, "
            "waste_json, cogs, food_cost_pct, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, wk, str(r["week_end"])[:10], r["inv_value"], waste.get("total_waste_cost"),
             json.dumps({k: waste[k] for k in ("total_waste_cost", "top_items", "inventory_value", "waste_rate_pct")
                         if k in waste}), cogs_v, pct, r["source"]))
        written += 1
        _ingredient_costs(conn, rid, r)
        conn.commit()
    return written


def _week_food_cost(restaurant_id, week_end):
    """(cogs, food cost %) for the seven days ending at `week_end`, from the
    counts, deliveries and sales still on file; (None, None) when any part
    is missing — never a figure computed from a substituted zero."""
    try:
        import cogs
        d = date.fromisoformat(str(week_end)[:10])
        out = cogs.build_food_cost_pct(restaurant_id, days=7, today=d)
        if out.get("ok"):
            return out.get("cogs"), out.get("pct")
    except Exception as e:
        log.debug("weekly food cost unavailable for %s %s: %s", restaurant_id, week_end, e)
    return None, None


def _ingredient_costs(conn, restaurant_id, row):
    """Fold one snapshot's per-item unit costs into the month's row."""
    try:
        items = json.loads(row["items_json"] or "[]") or []
    except (TypeError, ValueError):
        return
    month = str(row["week_end"])[:7]
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            cost = float(it.get("unit_cost"))
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        name = str(it.get("item") or it.get("name") or "").strip()
        iid = it.get("ingredient_id") or it.get("id")
        key = f"id:{iid}" if iid else ("name:" + name.lower())
        if key in ("name:",):
            continue
        prev = conn.execute("SELECT unit_cost_avg, n FROM ingredient_cost_monthly WHERE restaurant_id=? AND month=? "
                            "AND ingredient_key=?", (restaurant_id, month, key)).fetchone()
        n = int(prev["n"]) if prev else 0
        avg = ((float(prev["unit_cost_avg"] or 0) * n + cost) / (n + 1)) if prev else cost
        conn.execute(
            "INSERT INTO ingredient_cost_monthly (restaurant_id, month, ingredient_key, ingredient, unit, supplier, "
            "unit_cost_avg, unit_cost_last, n) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, month, "
            "ingredient_key) DO UPDATE SET unit_cost_avg=excluded.unit_cost_avg, unit_cost_last=excluded.unit_cost_last, "
            "n=excluded.n, ingredient=excluded.ingredient",
            (restaurant_id, month, key, name or None, it.get("unit"), it.get("supplier_name"), round(avg, 4),
             round(cost, 4), n + 1))


def inventory_summary_weeks(restaurant_id, since=None, until=None, conn=None, db_path=None) -> list:
    """The weekly summaries for weeks whose raw snapshots are gone:
    [{week_end, waste_json, inv_value}] — the shape waste_trend's loader
    builds a chart week from, so every reader of the loader (the waste
    trend, food cost % and the seasonal re-check behind it) sees last year
    and beyond."""
    own = conn is None
    c = _conn(db_path) if own else conn
    sql, args = ("SELECT week_end, waste_json, inv_value FROM inventory_weekly_summary WHERE restaurant_id=?",
                 [restaurant_id])
    if since:
        sql += " AND week_end >= ?"
        args.append(str(since)[:10])
    if until:
        sql += " AND week_end <= ?"
        args.append(str(until)[:10])
    try:
        return [dict(r) for r in c.execute(sql + " ORDER BY week_end", args).fetchall()]
    except Exception:
        return []
    finally:
        if own:
            c.close()


def ingredient_cost_history(restaurant_id, ingredient, db_path=None) -> list:
    """"What did salmon cost two years ago?" — the month-by-month unit cost
    of one ingredient (matched by id or name, case-insensitive), oldest
    first: [{month, unit, supplier, unit_cost_avg, unit_cost_last, n}]."""
    conn = _conn(db_path)
    try:
        key = str(ingredient).strip()
        rows = conn.execute(
            "SELECT month, ingredient, unit, supplier, unit_cost_avg, unit_cost_last, n FROM ingredient_cost_monthly "
            "WHERE restaurant_id=? AND (ingredient_key=? OR LOWER(ingredient)=LOWER(?)) ORDER BY month",
            (restaurant_id, key if key.startswith(("id:", "name:")) else f"id:{key}", key)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
