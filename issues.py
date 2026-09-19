"""
issues.py — someone owns it, someone saw it, someone closed it.

A regional manager asked for this in so many words: "AI alerts via phone to
hold local managers accountable." The product could already text a phone
(alert_contacts), but an alert is a broadcast — nothing recorded who was
responsible, whether they had seen it, whether it was dealt with, or who to
tell when it wasn't. The owner delegated bad reviews by forwarding emails.

An issue here has an assignee (a consented alert contact), a token link the
assignee opens from the SMS without needing a Cavnar login, and three states:
open -> acknowledged -> resolved. An issue nobody acknowledges within the
routing's window escalates, once, to the escalation contact.

What becomes an issue automatically is deliberately narrow: a 1-2 star review
at a location that has a manager routed. Loss-prevention signals are NEVER
auto-routed to a manager — the approving manager may be the subject of one.
Anything else is created by the owner, from the app or from Ask.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from models import get_conn, DB_PATH

DEFAULT_ESCALATE_MINUTES = 120
AUTO_REVIEW_MAX_RATING = 2
AUTO_REVIEW_LOOKBACK_HOURS = 48
# The review itself must be recent, not just recently fetched: connecting
# Google imports the whole review history with a fresh fetched_at, and every
# old 1-star review would otherwise text the manager on day one.
AUTO_REVIEW_MAX_AGE_DAYS = 3
# And however many qualify, one scan opens at most this many — a bad night
# is one conversation with the manager, not a dozen texts.
AUTO_REVIEW_MAX_PER_SCAN = 3
ROLES = ("manager", "escalation")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _base_url():
    return (os.getenv("BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ── routing ────────────────────────────────────────────────────────────────

def get_routing(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT r.role, r.contact_id, r.escalate_after_minutes, c.name, c.phone "
            "FROM issue_routing r JOIN alert_contacts c ON c.id=r.contact_id "
            "AND c.restaurant_id=r.restaurant_id AND COALESCE(c.sms_consent,0)=1 "
            "WHERE r.restaurant_id=?", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return {r["role"]: {"contact_id": r["contact_id"], "name": r["name"], "phone": r["phone"],
                        "escalate_after_minutes": r["escalate_after_minutes"] or DEFAULT_ESCALATE_MINUTES}
            for r in rows}


def set_routing(restaurant_id, role, contact_id, escalate_after_minutes=None, db_path=DB_PATH):
    """Point a role at one of THIS restaurant's alert contacts.

    The contact must belong to this restaurant — checked here rather than
    trusted from the caller, so one location can never route its issues to
    another location's staff.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    conn = get_conn(db_path)
    try:
        # Consent is checked, not assumed: this is the same rule notify.py
        # applies to every alert text (get_alert_contacts(sms_consent_only=
        # True)), and admin-added contacts never carry it. Routing an issue
        # to someone who never agreed to be texted is a TCPA problem, not a
        # configuration choice.
        ok = conn.execute("SELECT 1 FROM alert_contacts WHERE id=? AND restaurant_id=? "
                          "AND COALESCE(sms_consent,0)=1", (contact_id, restaurant_id)).fetchone()
        if not ok:
            raise ValueError("that contact is not a consented alert contact at this restaurant")
        mins = int(escalate_after_minutes or DEFAULT_ESCALATE_MINUTES)
        mins = max(15, min(mins, 24 * 60))
        conn.execute(
            "INSERT INTO issue_routing (restaurant_id, role, contact_id, escalate_after_minutes, updated_at) "
            "VALUES (?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, role) DO UPDATE SET "
            "contact_id=excluded.contact_id, escalate_after_minutes=excluded.escalate_after_minutes, "
            "updated_at=excluded.updated_at", (restaurant_id, role, contact_id, mins))
        conn.commit()
    finally:
        conn.close()
    return get_routing(restaurant_id, db_path)


def clear_routing(restaurant_id, role, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM issue_routing WHERE restaurant_id=? AND role=?", (restaurant_id, role))
        conn.commit()
    finally:
        conn.close()


# ── lifecycle ──────────────────────────────────────────────────────────────

def _public(row):
    return dict(row)


def _mint_link(issue_id, contact_id, purpose="assignee", db_path=DB_PATH):
    """A new link for one person. Only its hash is stored; the token itself
    exists only in the text message that carries it."""
    token = secrets.token_urlsafe(24)
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO issue_links (token_hash, issue_id, contact_id, purpose) VALUES (?,?,?,?)",
                     (_hash(token), issue_id, contact_id, purpose))
        conn.commit()
    finally:
        conn.close()
    return token


def create_issue(restaurant_id, kind, title, detail=None, severity="normal", source_key=None,
                 assignee_contact_id=None, created_by=None, notify=True, db_path=DB_PATH):
    """Open an issue, assign it, and text the assignee a link.

    Idempotent on source_key: the same review can never open two issues,
    however many times the scan that finds it runs. Returns (issue, token);
    the token is only ever returned at creation — only its hash is stored.
    """
    if severity not in ("high", "normal"):
        severity = "normal"
    routing = get_routing(restaurant_id, db_path)
    if assignee_contact_id is None and "manager" in routing:
        assignee_contact_id = routing["manager"]["contact_id"]
    assignee_name = None
    if assignee_contact_id is not None:
        conn = get_conn(db_path)
        try:
            c = conn.execute("SELECT name FROM alert_contacts WHERE id=? AND restaurant_id=? "
                             "AND COALESCE(sms_consent,0)=1",
                             (assignee_contact_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        if not c:
            raise ValueError("that contact is not a consented alert contact at this restaurant")
        assignee_name = c["name"]

    conn = get_conn(db_path)
    try:
        if source_key:
            existing = conn.execute("SELECT * FROM ops_issues WHERE restaurant_id=? AND source_key=?",
                                    (restaurant_id, source_key)).fetchone()
            if existing:
                return _public(existing), None
        cur = conn.execute(
            "INSERT INTO ops_issues (restaurant_id, kind, source_key, title, detail, severity, "
            "assignee_contact_id, assignee_name, status, created_by, escalation_contact_id) "
            "VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (restaurant_id, kind, source_key, title[:200], (detail or "")[:2000] or None, severity,
             assignee_contact_id, assignee_name, created_by,
             (routing.get("escalation") or {}).get("contact_id")))
        conn.commit()
        issue_id = cur.lastrowid
    finally:
        conn.close()
    # Into the notification history whether or not a text goes out — an
    # issue the owner can't find in the bell may as well not exist, and the
    # SMS only ever reaches the routed manager. (`notify` is a bool
    # parameter here, hence the aliased import.)
    import notify as _notify_mod
    _notify_mod.record_notification(restaurant_id,
                                    "coverage" if kind == "coverage" else "issue",
                                    db_path=db_path)
    # No assignee, no link: a link is a credential for one person, and an
    # unassigned issue has nobody to hold it. Assigning it later mints one.
    token = _mint_link(issue_id, assignee_contact_id, db_path=db_path) \
        if assignee_contact_id is not None else None
    if notify and token:
        _notify(issue_id, token, db_path=db_path)
    return _public(get_issue(restaurant_id, issue_id, db_path)), token


def _restaurant_name(restaurant_id):
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return (r.location_name or r.name) if r else "your restaurant"


def _sendable(issue_id, db_path=DB_PATH):
    """The issue row with its assignee's phone when a text may go out NOW,
    else None: no consented phone, already notified, resolved, or quiet hours
    (held, not dropped — the tick sends it when they end)."""
    from models import is_in_quiet_hours
    conn = get_conn(db_path)
    try:
        r = conn.execute(
            "SELECT i.*, c.phone FROM ops_issues i LEFT JOIN alert_contacts c ON c.id=i.assignee_contact_id "
            "AND c.restaurant_id=i.restaurant_id AND COALESCE(c.sms_consent,0)=1 "
            "WHERE i.id=?", (issue_id,)).fetchone()
    finally:
        conn.close()
    if not r or not r["phone"] or r["notified_at"] or r["status"] == "resolved":
        return None
    if is_in_quiet_hours(r["restaurant_id"], db_path=db_path):
        return None
    return r


def _notify(issue_id, token, db_path=DB_PATH):
    """Text the assignee their link. Returns True when a text went out."""
    r = _sendable(issue_id, db_path)
    if not r or not token:
        return False
    from notify import send_sms
    where = _restaurant_name(r["restaurant_id"])
    msg = (f"Cavnar AI · {where}: {r['title']}. Assigned to you — "
           f"tap to respond: {_base_url()}/i/{token}")
    sent = send_sms(r["phone"], msg[:320], use_case="alert")
    if sent:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET notified_at=? WHERE id=?", (_now(), issue_id))
            conn.commit()
        finally:
            conn.close()
    return bool(sent)


def get_issue(restaurant_id, issue_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM ops_issues WHERE id=? AND restaurant_id=?",
                         (issue_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    return _public(r) if r else None


def by_token(token, db_path=DB_PATH):
    """The issue a link points to, with its restaurant name — or None.

    Resolved issues still resolve (so an old link shows "already closed"
    rather than a 404 that reads as a broken link)."""
    if not token or len(token) < 20:
        return None
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT i.*, l.purpose AS link_purpose FROM issue_links l "
                         "JOIN ops_issues i ON i.id=l.issue_id WHERE l.token_hash=?",
                         (_hash(token),)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = _public(r)
    d["restaurant_name"] = _restaurant_name(r["restaurant_id"])
    return d


def acknowledge(token, db_path=DB_PATH):
    issue = by_token(token, db_path)
    if not issue:
        return None
    if issue["status"] == "open":
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET status='acknowledged', acknowledged_at=? "
                         "WHERE id=? AND status='open'", (_now(), issue["id"]))
            conn.commit()
        finally:
            conn.close()
    return by_token(token, db_path)


def _resolve(restaurant_id, issue_id, note, db_path):
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE ops_issues SET status='resolved', resolved_at=?, resolution_note=?, "
            "acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=? AND restaurant_id=? "
            "AND status!='resolved'", (_now(), (note or "")[:1000] or None, _now(), issue_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()


def resolve_by_token(token, note=None, db_path=DB_PATH):
    issue = by_token(token, db_path)
    if not issue:
        return None
    _resolve(issue["restaurant_id"], issue["id"], note, db_path)
    return by_token(token, db_path)


def resolve(restaurant_id, issue_id, note=None, db_path=DB_PATH):
    """The owner closing it from the app — scoped to their restaurant."""
    if not get_issue(restaurant_id, issue_id, db_path):
        return None
    _resolve(restaurant_id, issue_id, note, db_path)
    return get_issue(restaurant_id, issue_id, db_path)


def reassign(restaurant_id, issue_id, contact_id, db_path=DB_PATH):
    """Hand an issue to someone else. A fresh link goes out; the old one
    keeps working, so an assignee who already opened it is not locked out."""
    issue = get_issue(restaurant_id, issue_id, db_path)
    if not issue or issue["status"] == "resolved":
        return None
    conn = get_conn(db_path)
    try:
        c = conn.execute("SELECT name FROM alert_contacts WHERE id=? AND restaurant_id=? "
                         "AND COALESCE(sms_consent,0)=1", (contact_id, restaurant_id)).fetchone()
        if not c:
            raise ValueError("that contact is not a consented alert contact at this restaurant")
        conn.execute("UPDATE ops_issues SET assignee_contact_id=?, assignee_name=?, "
                     "notified_at=NULL, status='open', acknowledged_at=NULL, escalated_at=NULL "
                     "WHERE id=?", (contact_id, c["name"], issue_id))
        conn.commit()
    finally:
        conn.close()
    token = _mint_link(issue_id, contact_id, db_path=db_path)
    _notify(issue_id, token, db_path=db_path)
    return get_issue(restaurant_id, issue_id, db_path)


def list_issues(restaurant_id, status=None, limit=50, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        sql = "SELECT * FROM ops_issues WHERE restaurant_id=?"
        args = [restaurant_id]
        if status == "unresolved":
            sql += " AND status!='resolved'"
        elif status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 ELSE 2 END, id DESC LIMIT ?"
        args.append(int(limit))
        return [_public(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def summary(restaurant_id, db_path=DB_PATH):
    """Counts and the oldest unacknowledged issue — what the brief and the
    portfolio view show."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(status='open') AS open_n, SUM(status='acknowledged') AS ack_n, "
            "MIN(CASE WHEN status='open' THEN created_at END) AS oldest_open, "
            "SUM(status='resolved' AND resolved_at >= datetime('now','-7 days')) AS resolved_7d "
            "FROM ops_issues WHERE restaurant_id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    oldest_h = None
    if row and row["oldest_open"]:
        try:
            oldest_h = round((datetime.utcnow() - datetime.strptime(row["oldest_open"][:19],
                              "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600, 1)
        except ValueError:
            oldest_h = None
    return {"open": int(row["open_n"] or 0) if row else 0,
            "acknowledged": int(row["ack_n"] or 0) if row else 0,
            "resolved_last_7_days": int(row["resolved_7d"] or 0) if row else 0,
            "oldest_open_hours": oldest_h}


# ── the scheduler's side ───────────────────────────────────────────────────

def open_from_reviews(restaurant_id, db_path=DB_PATH):
    """Open an issue for each recent 1-2 star review at a location that has a
    manager routed. No routing, no issues — this is opt-in by configuring
    who owns them, not something that starts texting phones on its own."""
    if "manager" not in get_routing(restaurant_id, db_path):
        return []
    conn = get_conn(db_path)
    try:
        # datetime() on both sides: fetched_at is ISO with a 'T' and
        # review_date is sometimes a bare date, and a raw string comparison
        # against a space-separated cutoff is wrong for both.
        rows = conn.execute(
            "SELECT id, rating, author, text, specific_complaint FROM reviews WHERE restaurant_id=? "
            "AND deleted_at IS NULL AND rating<=? "
            "AND datetime(fetched_at) >= datetime('now', ?) "
            "AND datetime(review_date) >= datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM ops_issues o WHERE o.restaurant_id=reviews.restaurant_id "
            "                AND o.source_key='review:' || reviews.id) "
            "ORDER BY rating ASC, id DESC",
            (restaurant_id, AUTO_REVIEW_MAX_RATING, f"-{AUTO_REVIEW_LOOKBACK_HOURS} hours",
             f"-{AUTO_REVIEW_MAX_AGE_DAYS} days")).fetchall()
    finally:
        conn.close()
    opened = []
    for r in rows[:AUTO_REVIEW_MAX_PER_SCAN]:
        what = r["specific_complaint"] or (r["text"] or "")[:140]
        issue, token = create_issue(
            restaurant_id, "review",
            title=f"{r['rating']}★ review needs follow-up",
            detail=f"{r['author'] or 'A guest'}: {what}",
            severity="high" if (r["rating"] or 5) <= 1 else "normal",
            source_key=f"review:{r['id']}", db_path=db_path)
        if token:            # newly opened, not the dedupe path
            opened.append(issue)
    return opened


def tick(db_path=DB_PATH, now=None):
    """Every scheduler tick: send notifications held by quiet hours, and
    escalate issues nobody acknowledged in time. Each escalates at most once."""
    now = now or datetime.utcnow()
    from models import is_in_quiet_hours
    from notify import send_sms
    conn = get_conn(db_path)
    try:
        held = conn.execute("SELECT id, restaurant_id FROM ops_issues WHERE status='open' "
                            "AND notified_at IS NULL AND assignee_contact_id IS NOT NULL").fetchall()
        stale = conn.execute(
            "SELECT i.*, r.escalate_after_minutes, r.contact_id AS esc_contact_id, "
            "c.phone AS esc_phone, c.name AS esc_name "
            "FROM ops_issues i JOIN issue_routing r ON r.restaurant_id=i.restaurant_id AND r.role='escalation' "
            "JOIN alert_contacts c ON c.id=r.contact_id AND c.restaurant_id=i.restaurant_id "
            "AND COALESCE(c.sms_consent,0)=1 "
            "WHERE i.status='open' AND i.notified_at IS NOT NULL AND i.escalated_at IS NULL "
            # Escalating to the person who already has it texts them twice.
            "AND r.contact_id != COALESCE(i.assignee_contact_id, -1)").fetchall()
    finally:
        conn.close()

    sent_held = 0
    for h in held:
        # Checked BEFORE minting. A held issue's link was never sent and its
        # token is unrecoverable (only the hash is stored), so it needs a
        # fresh one — but only when a text can actually go out. Minting first
        # added a link row every tick for the whole of quiet hours, and
        # forever for an assignee whose consent was later withdrawn.
        r = _sendable(h["id"], db_path)
        if not r:
            continue
        token = _mint_link(h["id"], r["assignee_contact_id"], db_path=db_path)
        if _notify(h["id"], token, db_path=db_path):
            sent_held += 1

    escalated = 0
    for s in stale:
        try:
            notified = datetime.strptime(s["notified_at"][:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if now - notified < timedelta(minutes=int(s["escalate_after_minutes"] or DEFAULT_ESCALATE_MINUTES)):
            continue
        if is_in_quiet_hours(s["restaurant_id"], db_path=db_path):
            continue
        # The escalation contact gets their OWN link. The assignee's stays
        # valid — bringing in the regional manager must not lock the local
        # one out of the issue they were given.
        token = _mint_link(s["id"], s["esc_contact_id"], purpose="escalation", db_path=db_path)
        who = s["assignee_name"] or "the assigned manager"
        mins = int(s["escalate_after_minutes"] or DEFAULT_ESCALATE_MINUTES)
        msg = (f"Cavnar AI · {_restaurant_name(s['restaurant_id'])}: not acknowledged by {who} "
               f"after {mins} min — {s['title']}. {_base_url()}/i/{token}")
        if send_sms(s["esc_phone"], msg[:320], use_case="alert"):
            import notify as _notify_mod
            _notify_mod.record_notification(s["restaurant_id"], "issue_escalated", db_path=db_path)
            conn = get_conn(db_path)
            try:
                conn.execute("UPDATE ops_issues SET escalated_at=?, escalation_contact_id=? WHERE id=?",
                             (_now(), s["esc_contact_id"], s["id"]))
                conn.commit()
            finally:
                conn.close()
            escalated += 1
    return {"held_sent": sent_held, "escalated": escalated}


# How long after opening an unfinished opening checklist becomes an issue.
# Before this nothing watched them: the checklists existed, staff ticked
# them, and an owner only found out they hadn't by walking in.
CHECKLIST_GRACE_HOURS = 1


def open_from_signals(restaurant_id, db_path=DB_PATH, today=None):
    """Turn today's operational signals into owned work.

    Bad reviews became issues from the day the loop shipped; everything else
    stayed an alert nobody was accountable for. The same three signals the
    daily alerts already compute — what the kitchen is out of, labour over
    target, and money going in the bin — now land on the routed manager with
    a name against them.

    Needs a routed manager, like every other auto-issue: no routing, no
    issues. One per signal per day (source_key), so a signal that persists
    does not re-text anyone.
    """
    from datetime import date as _date
    if "manager" not in get_routing(restaurant_id, db_path):
        return []
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r:
        return []
    today = today or _date.today()
    stamp = today.isoformat()
    opened = []

    def _open(kind, title, detail, severity="normal", key=None):
        issue, token = create_issue(restaurant_id, kind, title, detail=detail, severity=severity,
                                    source_key=key, db_path=db_path)
        if token:
            opened.append(issue)

    if getattr(r, "module_inventory", 0):
        try:
            from inventory import load_inventory_for_restaurant, analysis_for
            items, is_live = load_inventory_for_restaurant(restaurant_id)
            if is_live and items:
                analysis = (analysis_for(restaurant_id, items=items, is_live=True) or (None, None, {}))[2]
                low = [x["item"] for x in (analysis.get("critical_low") or [])]
                if low:
                    _open("stock", f"{len(low)} item{'' if len(low) == 1 else 's'} critically low",
                          "Running out today: " + ", ".join(low[:8]) +
                          ("…" if len(low) > 8 else "") + ". Order or 86 before service.",
                          severity="high" if len(low) >= 3 else "normal",
                          key=f"stock:{stamp}")
        except Exception as e:
            _capture(e, "issue_signals_stock", restaurant_id)

    if getattr(r, "module_labor", 0):
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(restaurant_id) or {}
            target = float(getattr(r, "labor_target_pct", 30) or 30)
            pct = labor.get("labor_pct")
            if labor.get("is_live") and pct is not None and float(pct) - target >= 3:
                _open("labor", f"Labour {float(pct):.1f}% against a {target:.0f}% target",
                      "Last week ran over. Trim the overstaffed days in next week's schedule "
                      "before it is published.", key=f"labor:{today.strftime('%G-W%V')}")
        except Exception as e:
            _capture(e, "issue_signals_labor", restaurant_id)
    return opened


def open_from_checklists(restaurant_id, db_path=DB_PATH, now_local=None):
    """An opening checklist still unfinished an hour after opening becomes
    the manager's issue. Nothing watched these before."""
    from datetime import date as _date
    if "manager" not in get_routing(restaurant_id, db_path):
        return []
    from models import get_restaurant, get_todays_tasks
    r = get_restaurant(restaurant_id)
    if not r:
        return []
    from time_utils import restaurant_now
    local = now_local or restaurant_now(r, naive=True)
    from notify import _open_window
    window = _open_window(r, local.strftime("%A"))
    if not window or not window[0]:
        return []                      # hours not configured: nothing to be late for
    open_h, open_m = window[0]
    opened_at = local.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    from datetime import timedelta as _td
    if local < opened_at + _td(hours=CHECKLIST_GRACE_HOURS):
        return []                      # still inside the grace period
    conn = get_conn(db_path)
    try:
        roles = [x[0] for x in conn.execute(
            "SELECT DISTINCT role FROM task_templates WHERE restaurant_id=? AND is_active=1",
            (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    outstanding = []
    for role in roles:
        for t in get_todays_tasks(restaurant_id, role, task_date=local.date().isoformat(), db_path=db_path):
            if not t["done"]:
                outstanding.append(f"{role}: {t['label']}")
    if not outstanding:
        return []
    issue, token = create_issue(
        restaurant_id, "checklist",
        f"{len(outstanding)} opening task{'' if len(outstanding) == 1 else 's'} not ticked off",
        detail="Still open an hour after opening — " + "; ".join(outstanding[:8]) +
               ("…" if len(outstanding) > 8 else ""),
        source_key=f"checklist:{local.date().isoformat()}", db_path=db_path)
    return [issue] if token else []


def _capture(exc, job, restaurant_id):
    try:
        import ops
        ops.capture(exc, job=job, context=f"restaurant_id={restaurant_id}")
    except Exception:
        pass
