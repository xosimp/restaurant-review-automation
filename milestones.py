"""
milestones.py — moments worth marking, each marked once.

The delight audit found the product had exactly one celebration in it: a
modal that fires when every review has a reply (`dashboard.html`), gated on
`localStorage['cavnar_congrats_shown']`. It promised "this won't appear
again" and could not keep that promise — a new browser, a cleared cache or
the phone all re-fired it, and none of them could see the others. There was
no celebration of any kind on iOS.

A milestone here is a row, so the promise holds everywhere: the UNIQUE index
on (restaurant_id, key) is the once-only guarantee, and `fire()` treats the
IntegrityError as "already celebrated" exactly the way `ops.claim_period`
treats a lost claim.

WHAT EARNS A MILESTONE. Deliberately a short list, and every entry is a real
fact about the business rather than a usage statistic. "You logged in 50
times" is a milestone about us; "you have saved a measured $5,000" is a
milestone about them. The audit's own recommendation was to reject anything
that gamifies — no streaks of app-opening, no points, no badges, no levels.

Every milestone must satisfy three rules:

  1. IT IS TRUE AND MEASURED. The dollar milestones read
     `outcomes.total_value`, which counts only before-and-after measured
     results. Nothing here can fire off an estimate or an opportunity.

  2. IT FIRES ONCE, EVER. Not once a month, not once a browser.

  3. IT CAN BE EXPLAINED. Each carries the body text that says what it is
     counting, so an owner who asks "from what?" has the answer on screen.
"""
import json
import logging
import sqlite3

from models import DB_PATH, get_conn

log = logging.getLogger(__name__)

# Measured-dollar thresholds, ascending. Read from outcomes.total_value —
# the before-and-after figure — never from the opportunity or avoided ones,
# which are not money anybody made.
SAVINGS_TIERS = (1000, 5000, 10000, 25000, 50000, 100000)

# Months of service worth marking. 1 is deliberately absent: a first month
# is not an achievement, it is a trial ending.
ANNIVERSARY_MONTHS = (3, 6, 12, 24, 36)

KINDS = ("savings", "anniversary", "response_rate", "goal", "record", "streak")


def _table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS milestones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        kind TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL,
        body TEXT, value REAL, data TEXT,
        notified_at TEXT, seen_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')))""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_milestones ON milestones(restaurant_id, key)")


def fire(restaurant_id, kind, key, title, body=None, value=None, data=None,
         db_path=DB_PATH):
    """Record a milestone if it has never been recorded for this restaurant.

    Returns the new row, or None when it had already fired. The None is the
    whole point — every caller can run on every tick and only the first one
    ever produces anything.
    """
    conn = get_conn(db_path)
    try:
        _table(conn)
        conn.execute(
            "INSERT INTO milestones (restaurant_id, kind, key, title, body, value, data) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, kind, key, title, body, value,
             json.dumps(data) if data is not None else None))
        conn.commit()
    except sqlite3.IntegrityError:
        return None                      # already celebrated — the good case
    except Exception as e:
        # Fails open, and says so. A milestone is a nicety; losing one must
        # never take down the job that noticed it.
        log.warning("milestones.fire failed: %s", e)
        return None
    finally:
        conn.close()
    return get(restaurant_id, key, db_path=db_path)


def get(restaurant_id, key, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        _table(conn)
        row = conn.execute("SELECT * FROM milestones WHERE restaurant_id=? AND key=?",
                           (restaurant_id, key)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def recent(restaurant_id, limit=5, db_path=DB_PATH, unseen_only=False):
    conn = get_conn(db_path)
    try:
        _table(conn)
        sql = "SELECT * FROM milestones WHERE restaurant_id=?"
        if unseen_only:
            sql += " AND seen_at IS NULL"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        return [dict(r) for r in conn.execute(sql, (restaurant_id, int(limit))).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def mark_seen(restaurant_id, key, db_path=DB_PATH):
    """The owner has been shown it. Separate from `notified_at` on purpose:
    a push can go out while the app is closed, and the in-app moment should
    still happen the next time they open it."""
    conn = get_conn(db_path)
    try:
        _table(conn)
        n = conn.execute("UPDATE milestones SET seen_at=datetime('now') "
                         "WHERE restaurant_id=? AND key=? AND seen_at IS NULL",
                         (restaurant_id, key)).rowcount
        conn.commit()
        return n > 0
    except Exception:
        return False
    finally:
        conn.close()


def mark_notified(restaurant_id, key, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        _table(conn)
        conn.execute("UPDATE milestones SET notified_at=datetime('now') "
                     "WHERE restaurant_id=? AND key=?", (restaurant_id, key))
        conn.commit()
    except Exception as e:
        # Losing this stamp costs a record of WHEN we told them, not the
        # once-only guarantee (that is the UNIQUE index on the row itself),
        # so it must not take down the job that just notified someone. It
        # still has to be visible: a database refusing writes looks exactly
        # like this, one harmless line at a time.
        import ops
        ops.capture(e, job="milestones", context=f"mark_notified {restaurant_id} {key}")
    finally:
        conn.close()


# ── the detectors ────────────────────────────────────────────────────────────

def check_savings(restaurant_id, db_path=DB_PATH):
    """Crossed a measured-dollar threshold.

    `outcomes.total_value` is annualised measured dollars — before-and-after
    results only. It deliberately does NOT include cost avoidance, surfaced
    alert dollars or opportunity, because none of those is money the
    restaurant made (see value_delivered.py).
    """
    import outcomes
    try:
        # total_value returns the honest shape (monthly, annual, and the
        # denominator of what could not be measured), never a bare number.
        # The tiers are annual, which is the figure an owner recognises.
        total = float((outcomes.total_value(restaurant_id, db_path=db_path) or {})
                      .get("annual") or 0)
    except Exception as e:
        log.warning("milestones.check_savings: %s", e)
        return None
    fired = None
    for tier in SAVINGS_TIERS:
        if total < tier:
            break
        m = fire(restaurant_id, "savings", f"savings:{tier}",
                 f"${tier:,} in measured results",
                 body=(f"Across the changes you tracked, Cavnar AI has measured "
                       f"${total:,.0f} a year in improvement. Every dollar of that came "
                       f"from a before-and-after on your own numbers — not an estimate."),
                 value=float(tier), db_path=db_path)
        if m:
            fired = m                     # keep the highest newly crossed tier
    return fired


def check_anniversary(restaurant_id, restaurant=None, today=None, db_path=DB_PATH):
    """Months of service, marked with what happened in them."""
    from datetime import date
    from models import get_restaurant
    from time_utils import parse_stored_dt
    today = today or date.today()
    restaurant = restaurant or get_restaurant(restaurant_id)
    if restaurant is None or not getattr(restaurant, "created_at", None):
        return None
    started = parse_stored_dt(restaurant.created_at)
    if started is None:
        return None
    started = started.date() if hasattr(started, "date") else started
    months = (today.year - started.year) * 12 + (today.month - started.month)
    if today.day < started.day:
        months -= 1
    if months not in ANNIVERSARY_MONTHS:
        return None
    label = "a year" if months == 12 else (f"{months // 12} years" if months % 12 == 0
                                           else f"{months} months")
    body = f"You have been running on Cavnar AI for {label}."
    try:
        import outcomes
        total = float(outcomes.total_value(restaurant_id, db_path=db_path) or 0)
        if total > 0:
            body += f" Measured results in that time: ${total:,.0f} a year."
    except Exception:
        pass
    return fire(restaurant_id, "anniversary", f"anniversary:{months}",
                f"{label.capitalize()} with Cavnar AI", body=body,
                value=float(months), db_path=db_path)


def check_response_rate(restaurant_id, rate=None, db_path=DB_PATH):
    """Every review on the dashboard has a reply.

    This is the celebration the product already had, moved off localStorage
    so the promise that it appears once is one the storage can keep.
    """
    if rate is None:
        try:
            from models import get_review_stats
            stats = get_review_stats(restaurant_id) or {}
            rate = float(stats.get("response_rate") or 0)
            if not stats.get("total"):
                return None
        except Exception as e:
            log.warning("milestones.check_response_rate: %s", e)
            return None
    if float(rate or 0) < 100:
        return None
    return fire(restaurant_id, "response_rate", "response_rate:100",
                "Every review answered",
                body=("Every review on your dashboard has a response. Guests who see "
                      "an owner reply come back more often, and it is the single "
                      "cheapest thing a restaurant can do about its rating."),
                value=100.0, db_path=db_path)


def check_goal(restaurant_id, goal, db_path=DB_PATH):
    """A goal the owner set has been met over a full trailing window.

    goals.progress already refuses to call a target met on one good day, so
    by the time this fires the target has held for the metric's own window.
    """
    if not goal or goal.get("state") != "met":
        return None
    import goals as _goals
    return fire(restaurant_id, "goal", f"goal:{goal.get('id')}",
                f"Goal met — {goal.get('label') or goal.get('metric')}",
                body=_goals.summarise(goal), value=goal.get("target"),
                data={"metric": goal.get("metric"), "goal_id": goal.get("id")},
                db_path=db_path)


def check_all(restaurant_id, restaurant=None, today=None, db_path=DB_PATH):
    """Every detector, for one restaurant. Returns what newly fired.

    Run from the daily job. Each detector is independently guarded so one
    that raises costs its own milestone and nothing else.
    """
    out = []
    for fn, args in ((check_savings, (restaurant_id,)),
                     (check_response_rate, (restaurant_id,))):
        try:
            m = fn(*args, db_path=db_path)
            if m:
                out.append(m)
        except Exception as e:
            log.warning("milestones.check_all %s: %s", getattr(fn, "__name__", fn), e)
    try:
        m = check_anniversary(restaurant_id, restaurant=restaurant, today=today,
                              db_path=db_path)
        if m:
            out.append(m)
    except Exception as e:
        log.warning("milestones.check_all anniversary: %s", e)
    try:
        import goals as _goals
        for g in (_goals.progress(restaurant_id, db_path=db_path, today=today) or []):
            m = check_goal(restaurant_id, g, db_path=db_path)
            if m:
                out.append(m)
    except Exception as e:
        log.warning("milestones.check_all goals: %s", e)
    return out
