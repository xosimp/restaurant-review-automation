"""learning_scorecard — is Cavnar AI getting better at THIS restaurant?
(memory audit 9/29/26, "scorecard").

Only the schedule measured whether the AI improved for a restaurant (draft
acceptance with a trend); the owner reports were trailing snapshots, and
fatigue — an owner dismissing or ignoring three quarters of what they are
shown — was an admin number nothing acted on. A fatigued restaurant kept
receiving the same volume.

One row per restaurant and month (learning_scorecards, kept forever: it is
the learning curve), refreshed nightly for the month in progress by the
learning pass and frozen once a month has closed:

  reply_edit_rate    replies approved that month the owner edited (light,
                     heavy or rewrite) of those with a draft and an edit
                     reading — falls as the drafter learns the owner's voice
  acceptance         recommendations first shown that month taken (accepted,
                     done or the change made) of those settled
  measured_success   results evaluated that month that improved, of the
                     clear verdicts (rec_learning.learned_verdict)
  forecast_error     the mean miss of the forecasts scored that month, and
                     by kind (forecast_log)
  draft_acceptance   the share of generated schedule rows that went out
                     untouched, over the weeks published that month
                     (schedule_versions.acceptance)
  ask_helpful_rate   Ask answers rated helpful of those rated
  fatigue            recommendations settled that month dismissed or
                     ignored, of those settled (the admin funnel's line: 75%
                     of at least 20)

flags(): a curve WORSENING (the month against the two before, past its
threshold) or FLAT (three months inside its band while short of a good
level), each a line on the admin console; volume_limit(): a fatigued
restaurant is shown fewer cards on Home, fewer nightly-report actions and
fewer feed cards until it recovers.
"""
import json
import logging
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

log = logging.getLogger(__name__)

SCORECARD_VERSION = 1
FATIGUE_SHARE = 0.75
FATIGUE_MIN_N = 20              # admin_ops.RAS_MIN_N: below it a rate is noise
# The throttle's reading (memory re-audit 9/29/26, LOOPS-5 / QUALITY-8): the
# episodes SETTLED in the last FATIGUE_WINDOW_DAYS (by when they settled —
# an ignored card settles when it expires), or, when that is fewer than
# FATIGUE_MIN_N, the newest FATIGUE_MIN_N settled in FATIGUE_HORIZON_DAYS.
# It was the calendar month of first showing, so on the 1st every fatigued
# restaurant's flag reset to the new month's empty row and the throttle
# lifted whether or not the owner had recovered. The month's own fatigue
# figure stays in the curve (compute_month); only the throttle reads this.
FATIGUE_WINDOW_DAYS = 30
FATIGUE_HORIZON_DAYS = 60
FROZEN_AFTER_DAYS = 7           # a closed month is re-read this long for late data
# name: (better direction, worsening step, flat band, good level)
CURVES = {
    "reply_edit_rate": ("down", 0.10, 0.02, 0.20),
    "acceptance": ("up", 0.10, 0.02, 0.50),
    "measured_success": ("up", 0.15, 0.03, 0.50),
    "forecast_error": ("down", 5.0, 1.0, 10.0),
    "draft_acceptance": ("up", 0.10, 0.02, 0.80),
    "ask_helpful_rate": ("up", 0.15, 0.03, 0.80),
}
MIN_N = {"reply_edit_rate": 5, "acceptance": 10, "measured_success": 3, "forecast_error": 2,
         "draft_acceptance": 2, "ask_helpful_rate": 5}
CURVE_LABELS = {"reply_edit_rate": "reply edit rate", "acceptance": "recommendation acceptance",
                "measured_success": "measured success", "forecast_error": "forecast error",
                "draft_acceptance": "schedule draft acceptance", "ask_helpful_rate": "Ask helpful rate"}
# How many of each a fatigued restaurant is shown (the defaults: Home 3
# cards, the nightly report 5 actions, the feed's first screen).
THROTTLE = {"home_recs": 2, "dsr_actions": 3, "feed_cards": 2}


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_learning_scorecard(db_path: str = DB_PATH):
    """Boot DDL (models.init_db). Kept forever: the learning curve."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS learning_scorecards (
            restaurant_id   INTEGER NOT NULL,
            month           TEXT    NOT NULL,
            metrics_json    TEXT    NOT NULL,
            flags_json      TEXT,
            fatigued        INTEGER NOT NULL DEFAULT 0,
            version         INTEGER NOT NULL,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, month)
        )""")
        # The throttle's own reading, written on the month in progress
        # (rolling_fatigue): fatigue_now 0/1 and what it rests on.
        have = {r[1] for r in conn.execute("PRAGMA table_info(learning_scorecards)")}
        for col, typ in (("fatigue_now", "INTEGER"), ("fatigue_now_json", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE learning_scorecards ADD COLUMN {col} {typ}")
        conn.commit()
    finally:
        conn.close()


def _month_bounds(d):
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def _rate(k, n):
    return {"value": round(k / n, 3) if n else None, "k": int(k), "n": int(n)}


def _q(conn, sql, args):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:
        log.warning("learning_scorecard: read failed: %s", e)
        return []


def compute_month(restaurant_id, month_start, db_path=None) -> dict:
    """The month's curves: {"metrics": {name: {value, n, ...}},
    "forecast_error_by_kind": {kind: {value, n}}, "fatigue": {share, n,
    fatigued}}. Never raises."""
    start, end = _month_bounds(month_start)
    s, e = start.isoformat(), end.isoformat()
    e_stamp = f"{e} 23:59:59"
    out = {"metrics": {}, "forecast_error_by_kind": {}, "fatigue": None, "month": start.strftime("%Y-%m")}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        r = _q(conn, "SELECT SUM(CASE WHEN edit_category IN ('light','heavy','rewrite') THEN 1 ELSE 0 END) AS edited, "
                     "COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                     "AND draft_response IS NOT NULL AND edit_category IS NOT NULL "
                     "AND approved_at >= ? AND approved_at <= ?", (restaurant_id, s, e_stamp))
        if r:
            out["metrics"]["reply_edit_rate"] = _rate(r[0]["edited"] or 0, r[0]["n"] or 0)
        r = _q(conn, "SELECT SUM(helpful) AS k, COUNT(*) AS n FROM ask_feedback WHERE restaurant_id=? "
                     "AND created_at >= ? AND created_at <= ?", (restaurant_id, s, e_stamp))
        if r:
            out["metrics"]["ask_helpful_rate"] = _rate(r[0]["k"] or 0, r[0]["n"] or 0)
        rows = _q(conn, "SELECT kind, error_pct FROM forecast_log WHERE restaurant_id=? AND error_pct IS NOT NULL "
                        "AND horizon_end >= ? AND horizon_end <= ?", (restaurant_id, s, e))
        by = {}
        for x in rows:
            by.setdefault(x["kind"], []).append(float(x["error_pct"]))
        for kind, vals in by.items():
            out["forecast_error_by_kind"][kind] = {"value": round(sum(vals) / len(vals), 1), "n": len(vals)}
        allv = [v for vals in by.values() for v in vals]
        out["metrics"]["forecast_error"] = {"value": round(sum(allv) / len(allv), 1) if allv else None,
                                            "n": len(allv)}
        trs = _q(conn, "SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND status='evaluated' "
                       "AND evaluate_on >= ? AND evaluate_on <= ?", (restaurant_id, s, e))
    finally:
        conn.close()
    try:
        import rec_learning
        k = n = 0
        for t in trs:
            t = dict(t)
            if str(t.get("source_key") or "").startswith(rec_learning.INFORMATIONAL_PREFIXES):
                continue
            v = rec_learning.learned_verdict(t.get("verdict"), t)
            if v in rec_learning.CLEAR_VERDICTS:
                n += 1
                k += 1 if v == "improved" else 0
        out["metrics"]["measured_success"] = _rate(k, n)
    except Exception as ex:
        log.warning("learning_scorecard: success unreadable for rid=%s: %s", restaurant_id, ex)
    try:
        import rec_learning
        conn = get_conn(db_path)
        try:
            eps = rec_learning._load(conn, restaurant_id, since=f"{s} 00:00:00", lean=True)
        finally:
            conn.close()
        eps = [x for x in eps if x.get("shown") and str(x.get("created_at") or "") <= e_stamp
               and x.get("state") != "superseded"]
        settled = [x for x in eps if rec_learning._settled(x)]
        taken = [x for x in settled if rec_learning._taken(x)]
        out["metrics"]["acceptance"] = _rate(len(taken), len(settled))
        lost = [x for x in settled if x.get("state") in ("dismissed", "ignored")]
        share = round(len(lost) / len(settled), 3) if settled else None
        out["fatigue"] = {"share": share, "n": len(settled),
                          "fatigued": bool(settled and len(settled) >= FATIGUE_MIN_N and share >= FATIGUE_SHARE)}
    except Exception as ex:
        log.warning("learning_scorecard: acceptance unreadable for rid=%s: %s", restaurant_id, ex)
    try:
        import schedule_versions
        kw = {"db_path": db_path} if db_path else {}
        acc = schedule_versions.acceptance(restaurant_id, weeks=12, **kw)
        shares = [w["unchanged_share"] for w in acc.get("weeks") or []
                  if w.get("unchanged_share") is not None and s <= str(w.get("week_start") or "") <= e]
        out["metrics"]["draft_acceptance"] = {"value": round(sum(shares) / len(shares), 3) if shares else None,
                                              "n": len(shares)}
    except Exception as ex:
        log.warning("learning_scorecard: draft acceptance unreadable for rid=%s: %s", restaurant_id, ex)
    return out


def _settled_on(ep):
    """When an episode settled: an answer's close (or the answer itself), an
    ignored card's expiry (created + rec_ledger.EXPIRE_AFTER_DAYS when the
    nightly expiry has not closed it yet). None for one still unsettled."""
    import rec_ledger
    import rec_learning
    if not rec_learning._settled(ep):
        return None
    if ep.get("state") == "ignored" and ep.get("status") != "expired":
        start = rec_ledger.chain_start(ep)
        try:
            return (datetime.strptime(str(start)[:19], "%Y-%m-%d %H:%M:%S")
                    + timedelta(days=rec_ledger.EXPIRE_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None
    if ep.get("closed_at"):
        return str(ep["closed_at"])[:19]
    answers = [e["at"] for e in ep.get("events") or () if e.get("event") in rec_ledger.TERMINAL]
    return str(answers[-1])[:19] if answers else str(ep.get("last_event_at") or ep.get("created_at") or "")[:19]


def rolling_fatigue(restaurant_id, today=None, db_path=None) -> dict:
    """The fatigue throttle's reading for `today`: {"fatigued", "share",
    "n", "window_days", "since"} over the episodes that settled in the last
    FATIGUE_WINDOW_DAYS — or, below FATIGUE_MIN_N, the newest FATIGUE_MIN_N
    that settled in FATIGUE_HORIZON_DAYS — dismissed or ignored of settled
    (rec_learning's states: a distrust, a timing answer, a decline taken
    back and an unopened email are in no denominator). Never raises."""
    import rec_ledger
    import rec_learning
    today = today or date.today()
    # Stamps are UTC and `today` is a local day: a day's slack at the top
    # keeps an evening's answers (already tomorrow in UTC) in the reading.
    end = f"{(today + timedelta(days=1)).isoformat()} 23:59:59"
    horizon = (today - timedelta(days=FATIGUE_HORIZON_DAYS)).isoformat() + " 00:00:00"
    window = (today - timedelta(days=FATIGUE_WINDOW_DAYS)).isoformat() + " 00:00:00"
    created_since = (today - timedelta(days=FATIGUE_HORIZON_DAYS + rec_ledger.EXPIRE_AFTER_DAYS + 1)).isoformat()
    out = {"fatigued": False, "share": None, "n": 0, "window_days": FATIGUE_WINDOW_DAYS, "since": None}
    try:
        conn = get_conn(db_path)
        try:
            eps = rec_learning._load(conn, restaurant_id, since=f"{created_since} 00:00:00", lean=True)
        finally:
            conn.close()
    except Exception as ex:
        log.warning("learning_scorecard: rolling fatigue unreadable for rid=%s: %s", restaurant_id, ex)
        return out
    settled = []
    for x in eps:
        if not x.get("shown") or x.get("state") == "superseded":
            continue
        on = _settled_on(x)
        if on and horizon <= on <= end:
            settled.append((on, x))
    settled.sort(key=lambda p: p[0], reverse=True)
    recent = [p for p in settled if p[0] >= window]
    if len(recent) < FATIGUE_MIN_N:
        recent = settled[:FATIGUE_MIN_N]
    if not recent:
        return out
    lost = sum(1 for _on, x in recent if x.get("state") in ("dismissed", "ignored"))
    share = round(lost / len(recent), 3)
    out.update({"share": share, "n": len(recent), "since": recent[-1][0][:10],
                "fatigued": bool(len(recent) >= FATIGUE_MIN_N and share >= FATIGUE_SHARE)})
    return out


def flags(rows) -> list:
    """[{curve, label, state: worsening | flat, latest, before, months}] from
    a restaurant's scorecards (oldest first, dicts with "metrics"). A curve
    is read only over months with at least MIN_N behind it."""
    out = []
    for name, (better, step, band, good) in CURVES.items():
        pts = []
        for r in rows:
            m = (r.get("metrics") or {}).get(name) or {}
            if m.get("value") is not None and int(m.get("n") or 0) >= MIN_N[name]:
                pts.append((r.get("month"), float(m["value"])))
        if len(pts) < 3:
            continue
        (m1, a), (m2, b), (m3, c) = pts[-3:]
        before = (a + b) / 2.0
        worse = (c - before) if better == "down" else (before - c)
        at_good = (c <= good) if better == "down" else (c >= good)
        if worse >= step:
            out.append({"curve": name, "label": CURVE_LABELS[name], "state": "worsening", "latest": round(c, 3),
                        "before": round(before, 3), "months": [m1, m2, m3]})
        elif max(a, b, c) - min(a, b, c) <= band and not at_good:
            out.append({"curve": name, "label": CURVE_LABELS[name], "state": "flat", "latest": round(c, 3),
                        "before": round(before, 3), "months": [m1, m2, m3]})
    return out


def scorecards(restaurant_id, months=12, db_path=None) -> list:
    """The stored months, oldest first: [{month, metrics, forecast_error_by_kind,
    fatigue, flags, fatigued}]. Never raises."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return []
    try:
        rows = conn.execute("SELECT * FROM learning_scorecards WHERE restaurant_id=? ORDER BY month DESC LIMIT ?",
                            (restaurant_id, int(months))).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in reversed(rows):
        try:
            body = json.loads(r["metrics_json"] or "{}")
        except (TypeError, ValueError):
            body = {}
        try:
            fl = json.loads(r["flags_json"] or "[]")
        except (TypeError, ValueError):
            fl = []
        out.append(dict(body, month=r["month"], flags=fl, fatigued=bool(r["fatigued"])))
    return out


def snapshot(restaurant_id, today=None, db_path=None) -> dict:
    """Write this month's row, and last month's while it can still change
    (FROZEN_AFTER_DAYS), then the flags over the stored curve. Returns
    {"written", "flags"}. Never raises."""
    today = today or date.today()
    months = [today.replace(day=1)]
    prev = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    if (today - today.replace(day=1)).days < FROZEN_AFTER_DAYS:
        months.append(prev)
    written = 0
    for m in months:
        body = compute_month(restaurant_id, m, db_path=db_path)
        try:
            conn = get_conn(db_path)
            try:
                conn.execute(
                    "INSERT INTO learning_scorecards (restaurant_id, month, metrics_json, fatigued, version, computed_at) "
                    "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, month) DO UPDATE SET "
                    "metrics_json=excluded.metrics_json, fatigued=excluded.fatigued, version=excluded.version, "
                    "computed_at=excluded.computed_at",
                    (restaurant_id, body["month"], json.dumps({k: body[k] for k in
                                                               ("metrics", "forecast_error_by_kind", "fatigue")}),
                     1 if (body.get("fatigue") or {}).get("fatigued") else 0, SCORECARD_VERSION))
                conn.commit()
                written += 1
            finally:
                conn.close()
        except Exception as e:
            log.warning("learning_scorecard: month not stored for rid=%s: %s", restaurant_id, e)
    fl = flags(scorecards(restaurant_id, months=6, db_path=db_path))
    now = rolling_fatigue(restaurant_id, today=today, db_path=db_path)
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE learning_scorecards SET flags_json=?, fatigue_now=?, fatigue_now_json=? "
                         "WHERE restaurant_id=? AND month=?",
                         (json.dumps(fl), 1 if now["fatigued"] else 0, json.dumps(now), restaurant_id,
                          today.strftime("%Y-%m")))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("learning_scorecard: flags not stored for rid=%s: %s", restaurant_id, e)
    return {"written": written, "flags": len(fl)}


def fatigued(restaurant_id, db_path=None) -> bool:
    """Whether this restaurant is past the fatigue line, as the nightly pass
    last read it (rolling_fatigue, stored as fatigue_now on the latest row,
    this month's or last month's): the episodes settled in the last 30 days,
    never the calendar month — so the throttle no longer lifts itself on the
    1st (LOOPS-5). A row written before fatigue_now existed falls back to its
    month's flag. Never raises."""
    if not restaurant_id:
        return False
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT fatigued, fatigue_now FROM learning_scorecards WHERE restaurant_id=? "
                               "AND month >= ? ORDER BY month DESC LIMIT 1",
                               (restaurant_id, (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m"))
                               ).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    if not row:
        return False
    return bool(row["fatigue_now"] if row["fatigue_now"] is not None else row["fatigued"])


def volume_limit(restaurant_id, surface, default) -> int:
    """How many recommendations `surface` may show: `default`, or
    THROTTLE[surface] (never more than default) while the restaurant is
    fatigued — most of what it was shown went dismissed or ignored, so it is
    shown less, the strongest first. Never raises."""
    try:
        if surface in THROTTLE and fatigued(restaurant_id):
            return min(int(default), THROTTLE[surface])
    except Exception:
        pass
    return int(default)
