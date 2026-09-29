"""
automation_trust.py — when each automation earned the owner's trust, on
what, and when it lost it (memory audit 9/29/26, "trust_ledger").

Auto-approve trust for a review star band needs AUTO_APPROVE_TRUST_MIN
person approvals in 30 days (models.auto_approve_trust) and was re-measured
on every run with no memory: once a band was trusted, its replies posted
themselves, person approvals in that band stopped, and within about a month
the band dropped back to manual — silently, with nothing recording that it
had ever been earned or that it had run clean in the meantime.

Here each (restaurant, scope, subject) holds its state: `earned` with the
basis it was earned on, or `lapsed` with when and why. While a band is held
earned, an auto-posted reply the owner neither retracted nor edited within
WEAK_CREDIT_DAYS counts as weak evidence (WEAK_CREDIT_WEIGHT of a person's
approval) toward KEEPING it — never toward earning it, so the rule still
cannot grade itself into trust. A band that lapses is not dropped silently:
the trust payload names it (`lapsed`) so the owner is asked again.

Scopes: "reply_band" (subject "3" / "4" / "5"). Pure SQL, no model.
"""
import json
from datetime import datetime

import models as _models_mod
from models import DB_PATH

# An auto-posted reply is weak evidence once the owner has had this long to
# retract or edit it, weighted below a person's own approval.
WEAK_CREDIT_DAYS = 7
WEAK_CREDIT_WEIGHT = 0.5
SCOPES = ("reply_band",)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_automation_trust(db_path: str = DB_PATH):
    """Boot DDL (models.init_db) — never on a request path. One row per
    (restaurant, scope, subject); kept for good (a few rows a restaurant)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS automation_trust (
            restaurant_id  INTEGER NOT NULL,
            scope          TEXT NOT NULL,
            subject        TEXT NOT NULL,
            state          TEXT NOT NULL,
            earned_at      TEXT,
            basis          TEXT,
            lapsed_at      TEXT,
            lapse_reason   TEXT,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, scope, subject)
        )""")
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def states(restaurant_id, scope, db_path=DB_PATH) -> dict:
    """{subject: row dict} for one scope. Never raises."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return {}
    try:
        return {r["subject"]: dict(r) for r in conn.execute(
            "SELECT * FROM automation_trust WHERE restaurant_id=? AND scope=?", (restaurant_id, scope)).fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def transition(restaurant_id, scope, subject, trusted, basis=None, reason=None, db_path=DB_PATH):
    """Record a change of state, only when it IS a change: earned (with its
    basis) or lapsed (with its reason). Returns the state row after. Never
    raises — the ledger must not stop an automation's own check."""
    subject = str(subject)
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        row = conn.execute("SELECT * FROM automation_trust WHERE restaurant_id=? AND scope=? AND subject=?",
                           (restaurant_id, scope, subject)).fetchone()
        now = _now()
        if trusted and (row is None or row["state"] != "earned"):
            conn.execute("INSERT OR REPLACE INTO automation_trust (restaurant_id, scope, subject, state, earned_at, "
                         "basis, lapsed_at, lapse_reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (restaurant_id, scope, subject, "earned", now, json.dumps(basis or {})[:1000],
                          row["lapsed_at"] if row is not None else None,
                          row["lapse_reason"] if row is not None else None, now))
        elif not trusted and row is not None and row["state"] == "earned":
            conn.execute("UPDATE automation_trust SET state='lapsed', lapsed_at=?, lapse_reason=?, updated_at=? "
                         "WHERE restaurant_id=? AND scope=? AND subject=?",
                         (now, (reason or "")[:200] or None, now, restaurant_id, scope, subject))
        conn.commit()
        got = conn.execute("SELECT * FROM automation_trust WHERE restaurant_id=? AND scope=? AND subject=?",
                           (restaurant_id, scope, subject)).fetchone()
        return dict(got) if got else None
    except Exception as e:
        print(f"[automation_trust] transition not recorded for {restaurant_id}/{scope}/{subject}: {e}")
        return None
    finally:
        conn.close()


def clean_autoposts(restaurant_id, days=30, db_path=DB_PATH) -> dict:
    """{star: (clean, retracted)} — auto-approved replies in the window: those
    still posted, unedited, WEAK_CREDIT_DAYS after they went out (weak
    evidence), and those the owner retracted (a strong no). Never raises."""
    out = {}
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        for r in conn.execute(
                "SELECT rating, "
                "SUM(CASE WHEN response_status='posted' AND COALESCE(draft_edited,0)=0 "
                "     AND approved_at <= datetime('now', ?) THEN 1 ELSE 0 END) AS clean "
                "FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL AND response_action='auto_approved' "
                "AND approved_at >= datetime('now', ?) AND rating IN (3,4,5) GROUP BY rating",
                (f"-{int(WEAK_CREDIT_DAYS)} days", restaurant_id, f"-{int(days)} days")).fetchall():
            out[int(r["rating"])] = [int(r["clean"] or 0), 0]
        for r in conn.execute(
                "SELECT rating, COUNT(*) AS n FROM activity_log a JOIN reviews v ON v.id = "
                "CAST(json_extract(a.event_data, '$.review_id') AS INTEGER) "
                "WHERE a.restaurant_id=? AND a.event_type='review_retracted' "
                "AND substr(a.created_at, 1, 10) >= date('now', ?) "
                "AND v.rating IN (3,4,5) GROUP BY v.rating", (restaurant_id, f"-{int(days)} days")).fetchall():
            out.setdefault(int(r["rating"]), [0, 0])[1] = int(r["n"] or 0)
    except Exception as e:
        print(f"[automation_trust] auto-posts unreadable for {restaurant_id}: {e}")
    finally:
        conn.close()
    return {k: tuple(v) for k, v in out.items()}


def lapsed_items(restaurant_id, db_path=DB_PATH) -> list:
    """The automations whose trust lapsed and has not been earned back —
    what the owner is re-asked about: [{scope, subject, lapsed_on (M/D/YY),
    reason, text}]."""
    from time_utils import mdy
    out = []
    for scope in SCOPES:
        for subject, st in states(restaurant_id, scope, db_path=db_path).items():
            if st["state"] != "lapsed":
                continue
            when = mdy(str(st["lapsed_at"] or "")[:10]) if st.get("lapsed_at") else ""
            text = (f"{subject}-star replies went back to you for approval on {when}"
                    + (f" ({st['lapse_reason']})" if st.get("lapse_reason") else "")
                    + ". Approve a few more as they come and they will post on their own again.")
            out.append({"scope": scope, "subject": subject, "lapsed_on": when, "reason": st.get("lapse_reason"),
                        "text": text})
    return out
