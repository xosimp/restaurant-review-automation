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
import config
import secrets
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports):
    a patch of models.get_conn reaches this module too."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

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
    return config.base_url()


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
    """The issue as clients read it. `meta` is the parsed meta_json (a
    coverage issue's suggested covers and who was already asked), so a
    client can render "Ask Ana to cover" without parsing a string."""
    d = dict(row)
    import json as _json
    try:
        d["meta"] = _json.loads(d.get("meta_json") or "null") or {}
    except (TypeError, ValueError):
        d["meta"] = {}
    return d


def _issue_token(issue_id, contact_id, purpose="assignee"):
    """The link token for one person on one issue: an HMAC over (issue,
    person, purpose) with this install's kept issue-link secret.

    Derived rather than random so it is the SAME token every time (#91): a
    text retried after a failure, or sent by tick() and create_issue racing
    for the same issue, carries the same link, and the link row is written
    once. It used to be minted fresh per attempt — a bad number retried every
    five minutes added ~288 issue_links rows a day. Only the hash is stored;
    the token exists in the text message and nowhere else."""
    import base64
    import hmac as _hmac
    from models import kept_secret
    key = kept_secret("issue_links")
    if not key:
        raise RuntimeError("issue link secret unavailable")
    msg = f"issue:{int(issue_id)}:{int(contact_id or 0)}:{purpose}".encode()
    return base64.urlsafe_b64encode(_hmac.new(key, msg, hashlib.sha256).digest()).decode().rstrip("=")[:32]


def _mint_link(issue_id, contact_id, purpose="assignee", db_path=DB_PATH):
    """The link for one person on one issue — created once (INSERT OR
    IGNORE on its hash) however many times a text is retried."""
    token = _issue_token(issue_id, contact_id, purpose)
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO issue_links (token_hash, issue_id, contact_id, purpose) "
                     "VALUES (?,?,?,?)", (_hash(token), issue_id, contact_id, purpose))
        conn.commit()
    finally:
        conn.close()
    return token


def create_issue(restaurant_id, kind, title, detail=None, severity="normal", source_key=None,
                 assignee_contact_id=None, created_by=None, notify=True, meta=None, db_path=DB_PATH):
    """Open an issue, assign it, and text the assignee a link.

    Idempotent on source_key: the same review can never open two issues,
    however many times the scan that finds it runs. Returns (issue, token);
    the token is only ever returned at creation — only its hash is stored.

    notify=False is PERSISTED (ops_issues.notify_suppressed), not just
    honoured at creation. tick() texts every open, assigned issue whose
    notified_at is empty — which is exactly what a filed-not-texted issue
    looks like — so a comp/void flag naming a manager, filed deliberately
    without a text, reached the routed manager's phone on the next tick
    anyway. It still appears on Home and in the issue list.

    `meta` is structured detail the issue's page acts on (a coverage
    issue's suggested covers), stored as JSON.
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
        import json as _json
        cur = conn.execute(
            "INSERT INTO ops_issues (restaurant_id, kind, source_key, title, detail, severity, "
            "assignee_contact_id, assignee_name, status, created_by, escalation_contact_id, "
            "notify_suppressed, meta_json) "
            "VALUES (?,?,?,?,?,?,?,?, 'open', ?, ?, ?, ?)",
            (restaurant_id, kind, source_key, title[:200], (detail or "")[:2000] or None, severity,
             assignee_contact_id, assignee_name, created_by,
             (routing.get("escalation") or {}).get("contact_id"),
             0 if notify else 1, _json.dumps(meta)[:4000] if meta else None))
        conn.commit()
        issue_id = cur.lastrowid
    finally:
        conn.close()
    _note_missed(restaurant_id, kind, source_key or f"issue:{issue_id}", title, db_path)
    # Into the notification history whether or not a text goes out — an
    # issue the owner can't find in the bell may as well not exist, and the
    # SMS only ever reaches the routed manager. (`notify` is a bool
    # parameter here, hence the aliased import.)
    import notify as _notify_mod
    # The row names the issue (ref_kind / ref_id), so the bell reads it as
    # handled once the issue is resolved instead of urgent forever.
    _notify_mod.record_notification(restaurant_id,
                                    "coverage" if kind == "coverage" else "issue",
                                    db_path=db_path, ref_kind="issue", ref_id=issue_id)
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


def _sendable(issue_id, db_path=DB_PATH, now=None):
    """The issue row with its assignee's phone when a text may go out NOW,
    else None: no consented phone, already notified, resolved, given up on
    (notify_failed_at), waiting out a retry backoff (notify_next_at), or quiet
    hours (held, not dropped — the tick sends it when they end)."""
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
    if "notify_suppressed" in r.keys() and r["notify_suppressed"]:
        return None                      # filed deliberately without a text
    if "notify_failed_at" in r.keys() and r["notify_failed_at"]:
        return None                      # given up on; the owner was told instead
    next_at = r["notify_next_at"] if "notify_next_at" in r.keys() else None
    if next_at and next_at > _stamp(now):
        return None                      # backing off after a failed text
    if is_in_quiet_hours(r["restaurant_id"], db_path=db_path):
        return None
    return r


# A text to an issue's assignee is tried at most this many times, backing
# off between tries (#91). tick() used to re-text a rejected number every
# five minutes, forever, minting a new link row each time.
MAX_NOTIFY_ATTEMPTS = 4
NOTIFY_BACKOFF_MINUTES = (5, 15, 45)


def _stamp(at=None):
    return (at or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")


def _backoff_until(attempts, now=None):
    mins = NOTIFY_BACKOFF_MINUTES[min(max(attempts, 1), len(NOTIFY_BACKOFF_MINUTES)) - 1]
    return _stamp((now or datetime.utcnow()) + timedelta(minutes=mins))


def _text(phone, msg, restaurant_id):
    """Send one issue text and return the whole outcome (SmsResult) — through
    notify.send_sms, the path every sender and test stand-in shares."""
    import notify
    return notify.send_sms_outcome(phone, msg[:320], use_case="alert", restaurant_id=restaurant_id)


def _notify(issue_id, token=None, db_path=DB_PATH, now=None):
    """Text the assignee their link. Returns True when a text went out.

    CLAIMED before sending (#91, jobs #7): notified_at is set only by the
    caller that wins `UPDATE … WHERE notified_at IS NULL`, so create_issue
    and tick() racing for the same issue send one text, not two with two
    links. The link is the one derived for this person (_mint_link), made
    only once a text can go. A number that may not be texted — a STOP, no
    consent — is given up on at once; a failed text is released and retried
    with backoff, and after MAX_NOTIFY_ATTEMPTS, or on a failure Twilio says
    is permanent, the issue is marked notify_failed_at and the owner is told
    by push and email instead (_fall_back)."""
    r = _sendable(issue_id, db_path, now)
    if not r:
        return False
    import notify
    blocked = notify.sms_block_reason(r["phone"], r["restaurant_id"], "alert", db_path)
    if blocked:
        _give_up(r, blocked, db_path, now)
        return False
    conn = get_conn(db_path)
    try:
        won = conn.execute("UPDATE ops_issues SET notified_at=? WHERE id=? AND notified_at IS NULL "
                           "AND notify_failed_at IS NULL", (_stamp(now), issue_id)).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if not won:
        return False                     # another caller has it
    token = token or _mint_link(issue_id, r["assignee_contact_id"], db_path=db_path)
    where = _restaurant_name(r["restaurant_id"])
    msg = (f"Cavnar AI · {where}: {r['title']}. Assigned to you — "
           f"tap to respond: {_base_url()}/i/{token}")
    res = _text(r["phone"], msg, r["restaurant_id"])
    if res.ok:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET notify_error=NULL, notify_next_at=NULL WHERE id=?", (issue_id,))
            conn.commit()
        finally:
            conn.close()
        _present(r, "issue_sms", db_path)
        return True
    attempts = int((r["notify_attempts"] if "notify_attempts" in r.keys() else 0) or 0) + 1
    reason = res.error or res.status or "the text was not accepted"
    if res.permanent or attempts >= MAX_NOTIFY_ATTEMPTS:
        _give_up(r, reason, db_path, now, attempts=attempts)
    else:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET notified_at=NULL, notify_attempts=?, notify_error=?, "
                         "notify_next_at=? WHERE id=?",
                         (attempts, str(reason)[:300], _backoff_until(attempts, now), issue_id))
            conn.commit()
        finally:
            conn.close()
    return False


def _give_up(r, reason, db_path=DB_PATH, now=None, attempts=None):
    """The assignee will not be texted about this issue: record why
    (notify_error, notify_failed_at — tick() never retries it) and tell the
    owner by push and email so the issue still reaches someone (#91, #107)."""
    conn = get_conn(db_path)
    try:
        # Conditional, so two callers giving up on the same issue tell the
        # owner once.
        won = conn.execute("UPDATE ops_issues SET notified_at=NULL, notify_failed_at=?, notify_error=?, "
                           "notify_attempts=COALESCE(?, notify_attempts) WHERE id=? AND notify_failed_at IS NULL",
                           (_stamp(now), str(reason)[:300], attempts, r["id"])).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if won:
        _fall_back(r, r["assignee_name"] or "the assigned manager", reason, db_path)


def _fall_back(r, who, reason, db_path=DB_PATH):
    """Push and email the owner that `who` could not be texted about issue
    `r`. Never raises. Loss issues are never pushed beyond the logins
    permitted to read them (notify.alert_audience)."""
    title = f"Couldn't text {who}"
    body = f"{r['title']} — {reason}. Open the issue to follow up yourself."
    rid = r["restaurant_id"]
    try:
        import notify
        import push
        audience = notify.alert_audience(rid, ["issue"], db_path)
        if audience is None or audience:
            push.fire_push(rid, "issue", title, body[:220],
                           data={"issue_id": r["id"], "surface": "issue_fallback"},
                           db_path=db_path, user_ids=audience)
    except Exception as e:
        print(f"[issues] fallback push failed for issue {r['id']}: {e}")
    try:
        import emails
        from models import get_restaurant
        rest = get_restaurant(rid)
        to = getattr(rest, "owner_email", None)
        if to:
            import html as _h
            place = (getattr(rest, "location_name", None) or getattr(rest, "name", None) or "your restaurant")
            html = emails.report_shell(
                kicker=_h.escape(place), title=_h.escape(title), subtitle="",
                sections=[emails.report_paragraph(_h.escape(r["title"])),
                          emails.report_paragraph(_h.escape(f"The text to {who} did not go through: {reason}. "
                                                            "Nobody has been told about this issue yet."))],
                cta_label="Open the issue", cta_url=f"{_base_url()}/?tab=home")
            emails.deliver(email_type="send_issue_fallback_email", restaurant_id=rid, payload={
                "from": emails.sender("client"), "to": [to],
                "subject": f"{title} — {place}", "preheader": str(r["title"])[:120], "html": html})
    except Exception as e:
        print(f"[issues] fallback email failed for issue {r['id']}: {e}")


_KIND_MODULE = {"review": "reviews", "stock": "food", "labor": "labor", "coverage": "labor",
                "no_show": "labor", "checklist": "ops", "plan": "ops", "loss": "ops",
                "task_missed": "labor", "task_sheet": "labor", "task_pattern": "labor",
                "task_flag": "labor"}


def _issue_key(r):
    return r["source_key"] or f"issue:{r['id']}"


# Issues that are not a problem surfacing: a weekly plan item and a single
# guest's review routed to a manager (no recommendation could precede one
# review) — everything else is checked for a recommendation first.
# A skipped duty, or a reading an employee took out of range on a critical
# task-sheet line (task_flag, COM-10), is not a detection Cavnar AI missed.
_NOT_A_MISS = ("plan", "review", "task_missed", "task_sheet", "task_pattern", "task_flag")


def _note_missed(restaurant_id, kind, key, title, db_path=DB_PATH):
    """An issue opened on a subject no recommendation covered in the days
    before is a missed detection (rec_ledger.note_problem, ROI #44). A card
    handed to someone (home_brief.assign) is its own recommendation and is
    covered by construction. Never raises."""
    if kind in _NOT_A_MISS or str(key or "").startswith("review:"):
        return
    try:
        import rec_ledger
        rec_ledger.note_problem(restaurant_id, "issue", key, module=_KIND_MODULE.get(kind),
                                detail=str(title or "")[:200], db_path=db_path)
    except Exception as e:
        print(f"[issues] missed-detection note failed for {restaurant_id}: {e}")


def _present(r, surface, db_path=DB_PATH):
    """The issue went out on `surface` — one line in the recommendation
    trail. Never raises: measuring must not undo a text that already went."""
    try:
        import rec_ledger
        rec_ledger.present_many(r["restaurant_id"], [{
            "key": _issue_key(r), "module": _KIND_MODULE.get(r["kind"], "ops"), "title": r["title"]}],
            surface, db_path=db_path)
    except Exception as e:
        print(f"[issues] rec_ledger present failed: {e}")


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
        cur = conn.execute(
            "UPDATE ops_issues SET status='resolved', resolved_at=?, resolution_note=?, "
            "acknowledged_at=COALESCE(acknowledged_at, ?) WHERE id=? AND restaurant_id=? "
            "AND status!='resolved'", (_now(), (note or "")[:1000] or None, _now(), issue_id, restaurant_id))
        conn.commit()
        changed = cur.rowcount == 1
        row = conn.execute("SELECT source_key FROM ops_issues WHERE id=?", (issue_id,)).fetchone()
    finally:
        conn.close()
    if changed:
        try:
            import rec_ledger
            rec_ledger.record(restaurant_id, (row["source_key"] if row else None) or f"issue:{issue_id}",
                              "completed", surface="issue_sms", meta={"note": (note or "")[:200]} if note else None,
                              source_ref=f"issue:{issue_id}:resolved", db_path=db_path)
        except Exception as e:
            print(f"[issues] rec_ledger record failed: {e}")
    # Resolving the issue answers the recommendation that raised it. Before
    # this, a fixed problem stayed on Home as "worth your time" until the
    # owner also pressed Done there — two clicks for one fact. Coverage and
    # loss keys are operational, not recommendations; they have no card.
    key = (row["source_key"] if row else None) or ""
    if changed and key and not key.startswith(("coverage:", "loss:", "signal:")):
        try:
            import home_brief
            home_brief.dismiss(restaurant_id, key, kind="done")
        except Exception:
            pass


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
        # An owner handing it to someone by name is asking for them to be
        # told — that lifts a filed-without-a-text issue's suppression.
        # A new person starts a new try: the old assignee's failed texts and
        # backoff are theirs, not this person's.
        conn.execute("UPDATE ops_issues SET assignee_contact_id=?, assignee_name=?, "
                     "notified_at=NULL, status='open', acknowledged_at=NULL, escalated_at=NULL, "
                     "notify_suppressed=0, notify_attempts=0, notify_next_at=NULL, notify_error=NULL, "
                     "notify_failed_at=NULL, escalation_attempts=0, escalation_next_at=NULL, "
                     "escalation_error=NULL WHERE id=?", (contact_id, c["name"], issue_id))
        conn.commit()
    finally:
        conn.close()
    token = _mint_link(issue_id, contact_id, db_path=db_path)
    _notify(issue_id, token, db_path=db_path)
    return get_issue(restaurant_id, issue_id, db_path)


def viewer_sees_loss(user) -> bool:
    """Whether this login may read loss issues (comp/void concentration).
    They name the approving manager, and are meant for the owner — never for
    the routed manager who may be their subject. None is an internal caller
    (the scheduler, reports to the owner). Fails closed."""
    if user is None or (isinstance(user, dict) and user.get("is_admin")):
        return True
    try:
        from permissions import has_permission, LOSS_VIEW
        return bool(has_permission(user, LOSS_VIEW))
    except Exception:
        return False


# An issue built from a module's figures names the modules in its meta
# (meta["modules"], permissions.MODULE_VIEW_PERMISSIONS keys — the Monday
# plan tags each item with what it draws on): a login without one of those
# modules' view does not read it. The plan is assembled with the owner's
# food-cost view, and every console login read its food-cost items here
# (memory re-audit PEOPLE-14 / PROMPTS-8).
_ALL_MODULES = ("reviews", "labor", "inventory", "marketing", "intel")


def hidden_modules(user) -> frozenset:
    """The module keys whose tagged issues this login may not read. None
    (an internal caller) and an admin: none. Fails closed: all of them."""
    if user is None or (isinstance(user, dict) and user.get("is_admin")):
        return frozenset()
    try:
        from permissions import MODULE_VIEW_PERMISSIONS, has_permission
        return frozenset(k for k, perm in MODULE_VIEW_PERMISSIONS.items() if not has_permission(user, perm))
    except Exception:
        return frozenset(_ALL_MODULES)


def issue_modules(row) -> list:
    """The modules an issue row (or its public form) is tagged with."""
    import json as _json
    meta = (row or {}).get("meta")
    if not isinstance(meta, dict):
        try:
            meta = _json.loads((row or {}).get("meta_json") or "null") or {}
        except (TypeError, ValueError):
            meta = {}
    mods = meta.get("modules") if isinstance(meta, dict) else None
    return [str(m) for m in mods] if isinstance(mods, list) else []


def viewer_sees_issue(user, row) -> bool:
    """One issue, by the list's rules: a loss issue needs LOSS_VIEW, and a
    module-tagged one the view of every module it names."""
    if not row:
        return False
    if row.get("kind") == "loss" and not viewer_sees_loss(user):
        return False
    hide = hidden_modules(user)
    return not any(m in hide for m in issue_modules(row))


def _module_clause(hide_modules):
    """SQL excluding issues tagged with any of `hide_modules`."""
    hide = sorted({str(m) for m in hide_modules or () if m})
    if not hide:
        return "", []
    return (" AND NOT EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(ops_issues.meta_json) "
            "THEN ops_issues.meta_json ELSE '{}' END, '$.modules') j WHERE j.value IN ("
            + ",".join("?" for _ in hide) + "))"), hide


def list_issues(restaurant_id, status=None, limit=50, db_path=DB_PATH, sees_loss=True, hide_modules=()):
    """`sees_loss=False` leaves out kind='loss' — every listing a manager can
    read passes viewer_sees_loss(viewer) (re-audit A-8) — and
    `hide_modules` (hidden_modules(viewer)) the issues tagged with a module
    that login may not open."""
    conn = get_conn(db_path)
    try:
        sql = "SELECT * FROM ops_issues WHERE restaurant_id=?"
        args = [restaurant_id]
        if not sees_loss:
            sql += " AND kind!='loss'"
        _msql, _margs = _module_clause(hide_modules)
        sql += _msql
        args += _margs
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


def summary(restaurant_id, db_path=DB_PATH, sees_loss=True, hide_modules=()):
    """Counts and the oldest unacknowledged issue — what the brief and the
    portfolio view show. `sees_loss` and `hide_modules` as list_issues."""
    _msql, _margs = _module_clause(hide_modules)
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT SUM(status='open') AS open_n, SUM(status='acknowledged') AS ack_n, "
            "MIN(CASE WHEN status='open' THEN created_at END) AS oldest_open, "
            "SUM(status='resolved' AND resolved_at >= datetime('now','-7 days')) AS resolved_7d "
            "FROM ops_issues WHERE restaurant_id=?" + ("" if sees_loss else " AND kind!='loss'") + _msql,
            (restaurant_id, *_margs)).fetchone()
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


def _coverage_shift_over(issue, db_path=DB_PATH, now_local=None) -> bool:
    """A coverage issue ("coverage:<date>:<who>") whose day is behind the
    restaurant's current business date: the shift it was about is over."""
    if (issue["kind"] if "kind" in issue.keys() else None) != "coverage":
        return False
    parts = str(issue["source_key"] or "").split(":", 2)
    if len(parts) < 2:
        return False
    try:
        from models import get_restaurant
        from time_utils import business_date, restaurant_now
        r = get_restaurant(issue["restaurant_id"], db_path)
        today = business_date(r, now_local or restaurant_now(r, naive=True))
        return parts[1] < today.isoformat()
    except Exception as e:
        print(f"[issues] coverage date check failed for issue {issue['id']}: {e}")
        return False


def _suppress_notify(issue_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE ops_issues SET notify_suppressed=1 WHERE id=?", (issue_id,))
        conn.commit()
    finally:
        conn.close()


def tick(db_path=DB_PATH, now=None):
    """Every scheduler tick: send notifications held by quiet hours, and
    escalate issues nobody acknowledged in time. Each escalates at most once."""
    now = now or datetime.utcnow()
    now_s = _stamp(now)
    from models import is_in_quiet_hours
    from models import in_service_sql
    conn = get_conn(db_path)
    try:
        # Never an issue filed with notify=False: "not notified yet" and
        # "deliberately not texted" look identical in notified_at alone.
        # Only restaurants still in service: a churned or paused account's
        # managers were still texted held issues and escalations (A-27).
        # Never one given up on (notify_failed_at) or still backing off
        # after a failed text (notify_next_at, #91).
        held = conn.execute("SELECT i.id, i.restaurant_id, i.kind, i.source_key FROM ops_issues i "
                            "JOIN restaurants rs ON rs.id=i.restaurant_id "
                            "WHERE i.status='open' "
                            "AND i.notified_at IS NULL AND i.assignee_contact_id IS NOT NULL "
                            "AND COALESCE(i.notify_suppressed, 0)=0 AND i.notify_failed_at IS NULL "
                            "AND (i.notify_next_at IS NULL OR i.notify_next_at <= ?) AND "
                            + in_service_sql("rs.billing_status"), (now_s,)).fetchall()
        stale = conn.execute(
            "SELECT i.*, r.escalate_after_minutes, r.contact_id AS esc_contact_id, "
            "c.phone AS esc_phone, c.name AS esc_name "
            "FROM ops_issues i JOIN issue_routing r ON r.restaurant_id=i.restaurant_id AND r.role='escalation' "
            "JOIN alert_contacts c ON c.id=r.contact_id AND c.restaurant_id=i.restaurant_id "
            "AND COALESCE(c.sms_consent,0)=1 "
            "JOIN restaurants rs ON rs.id=i.restaurant_id "
            "WHERE i.status='open' AND i.notified_at IS NOT NULL AND i.escalated_at IS NULL "
            "AND COALESCE(i.escalation_attempts, 0) < ? "
            "AND (i.escalation_next_at IS NULL OR i.escalation_next_at <= ?) "
            # Escalating to the person who already has it texts them twice.
            "AND r.contact_id != COALESCE(i.assignee_contact_id, -1) AND "
            + in_service_sql("rs.billing_status"), (MAX_NOTIFY_ATTEMPTS, now_s)).fetchall()
    finally:
        conn.close()

    sent_held = 0
    for h in held:
        # "Bob hasn't clocked in", held by quiet hours, used to go out the
        # next morning about a shift that was over (A-28). Past its date it
        # is not sent at all — marked suppressed, so it is not retried.
        if _coverage_shift_over(h, db_path):
            _suppress_notify(h["id"], db_path)
            continue
        # _notify checks it can send, claims, and only then makes the link
        # — the one link this person has for this issue (_mint_link).
        if _notify(h["id"], db_path=db_path, now=now):
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
        if _coverage_shift_over(s, db_path):
            continue                    # nobody to cover any more (A-28)
        if _escalate(s, db_path, now):
            escalated += 1
    return {"held_sent": sent_held, "escalated": escalated}


def _escalate(s, db_path=DB_PATH, now=None):
    """Text the escalation contact about issue `s`. Claimed first (#91):
    escalated_at is set by the one caller that wins it and cleared again on
    a failure worth retrying, with backoff; a number that may not be texted,
    a permanent failure or the attempt cap ends it — the owner is told by
    push and email instead. Returns True when the text went out."""
    import notify
    rid = s["restaurant_id"]
    attempts = int((s["escalation_attempts"] if "escalation_attempts" in s.keys() else 0) or 0)
    blocked = notify.sms_block_reason(s["esc_phone"], rid, "alert", db_path)
    if blocked:
        _end_escalation(s, blocked, db_path)
        return False
    conn = get_conn(db_path)
    try:
        won = conn.execute("UPDATE ops_issues SET escalated_at=?, escalation_contact_id=? "
                           "WHERE id=? AND escalated_at IS NULL",
                           (_stamp(now), s["esc_contact_id"], s["id"])).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if not won:
        return False
    # The escalation contact gets their OWN link. The assignee's stays
    # valid — bringing in the regional manager must not lock the local
    # one out of the issue they were given.
    token = _mint_link(s["id"], s["esc_contact_id"], purpose="escalation", db_path=db_path)
    who = s["assignee_name"] or "the assigned manager"
    mins = int(s["escalate_after_minutes"] or DEFAULT_ESCALATE_MINUTES)
    msg = (f"Cavnar AI · {_restaurant_name(rid)}: not acknowledged by {who} "
           f"after {mins} min — {s['title']}. {_base_url()}/i/{token}")
    res = _text(s["esc_phone"], msg, rid)
    if res.ok:
        notify.record_notification(rid, "issue_escalated", db_path=db_path, ref_kind="issue", ref_id=s["id"])
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET escalation_error=NULL, escalation_next_at=NULL WHERE id=?",
                         (s["id"],))
            conn.commit()
        finally:
            conn.close()
        return True
    attempts += 1
    reason = res.error or res.status or "the text was not accepted"
    if res.permanent or attempts >= MAX_NOTIFY_ATTEMPTS:
        _end_escalation(s, reason, db_path, attempts=attempts)
    else:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET escalated_at=NULL, escalation_attempts=?, escalation_error=?, "
                         "escalation_next_at=? WHERE id=?",
                         (attempts, str(reason)[:300], _backoff_until(attempts, now), s["id"]))
            conn.commit()
        finally:
            conn.close()
    return False


def _end_escalation(s, reason, db_path=DB_PATH, attempts=None):
    """No escalation text will go: escalation_attempts at the cap (so tick
    never selects it again), escalated_at cleared (nobody was escalated to),
    the reason kept, and the owner told instead. Conditional, so it is said
    once."""
    conn = get_conn(db_path)
    try:
        won = conn.execute("UPDATE ops_issues SET escalated_at=NULL, escalation_attempts=?, escalation_error=? "
                           "WHERE id=? AND COALESCE(escalation_attempts, 0) < ?",
                           (MAX_NOTIFY_ATTEMPTS, str(reason)[:300], s["id"], MAX_NOTIFY_ATTEMPTS)).rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if won:
        _fall_back(s, s["esc_name"] or "the escalation contact", reason, db_path)


# How long after opening an unfinished opening checklist becomes an issue.
# Before this nothing watched them: the checklists existed, staff ticked
# them, and an owner only found out they hadn't by walking in.
CHECKLIST_GRACE_HOURS = 1


def open_from_signals(restaurant_id, db_path=DB_PATH, today=None):
    """Turn today's operational signals into owned work.

    Bad reviews became issues from the day the loop shipped; everything else
    stayed an alert nobody was accountable for. The same three signals the
    daily alerts already compute — what the kitchen is out of, labor over
    target, and money going in the bin — now land on the routed manager with
    a name against them.

    Needs a routed manager, like every other auto-issue: no routing, no
    issues. Labor is one per ISO week (source_key); stock is one OPEN issue
    at a time, updated while it persists and closed when it clears
    (_sync_stock_issue), so a signal that persists does not re-text anyone.
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
                issue = _sync_stock_issue(restaurant_id, low, stamp, db_path)
                if issue:
                    opened.append(issue)
        except Exception as e:
            _capture(e, "issue_signals_stock", restaurant_id)

    if getattr(r, "module_labor", 0):
        try:
            from labor import analyse_shifts_for_restaurant
            from thresholds import LABOR_OVER_TARGET_PTS
            import notify as _notify_mod
            labor = analyse_shifts_for_restaurant(restaurant_id) or {}
            target = _notify_mod.labor_target_for(r, db_path=db_path)
            # overall_labor_pct is what analyse_shifts returns; this read
            # "labor_pct", a key it never had, so the labor issue could not
            # open (#34). One threshold with the alert and Home.
            pct = labor.get("overall_labor_pct")
            # Not on Cavnar's unconfirmed starting target (Benchmarking audit #13).
            import thresholds as _thr_t
            # The margin fitted to this restaurant's own swing, never below
            # the stated one (restaurant_thresholds, memory audit 9/29/26).
            import restaurant_thresholds as _rthr_iss
            _over = _rthr_iss.margin(restaurant_id, "labor_over_period", stated=LABOR_OVER_TARGET_PTS)
            if labor.get("is_live") and pct is not None and float(pct) - target >= _over \
                    and _thr_t.target_alerts_allowed(r, "labor"):
                _open("labor", f"Labor {float(pct):.1f}% against a {target:.0f}% target",
                      "The latest labor data ran over. Trim the overstaffed days in next week's "
                      "schedule before it is published.", key=f"labor:{today.strftime('%G-W%V')}")
        except Exception as e:
            _capture(e, "issue_signals_labor", restaurant_id)
    return opened


def _open_issue_of_kind(conn, restaurant_id, kind):
    return conn.execute("SELECT * FROM ops_issues WHERE restaurant_id=? AND kind=? AND status!='resolved' "
                        "ORDER BY id DESC LIMIT 1", (restaurant_id, kind)).fetchone()


def _sync_stock_issue(restaurant_id, low, stamp, db_path=DB_PATH):
    """ONE open stock issue, kept current — not a new one every day.

    The key used to be stock:{date}, so an item that stayed low opened a new
    issue and texted the manager again every morning (#18). Now: while one is
    open it is updated in place with today's list (no text — the manager
    already has it); when nothing is critically low any more it closes
    itself; only when none is open does a new one open (and text).
    Returns the newly opened issue, else None."""
    import json as _json
    conn = get_conn(db_path)
    try:
        current = _open_issue_of_kind(conn, restaurant_id, "stock")
    finally:
        conn.close()
    if not low:
        if current:
            _resolve(restaurant_id, current["id"], "Closed automatically: everything is back above par.", db_path)
        return None
    title = f"{len(low)} item{'' if len(low) == 1 else 's'} critically low"
    detail = ("Running out today: " + ", ".join(low[:8]) + ("…" if len(low) > 8 else "") +
              ". Order or 86 before service.")
    severity = "high" if len(low) >= 3 else "normal"
    if current:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE ops_issues SET title=?, detail=?, severity=?, meta_json=? WHERE id=?",
                         (title, detail, severity, _json.dumps({"items": low[:40]}), current["id"]))
            conn.commit()
        finally:
            conn.close()
        return None
    issue, token = create_issue(restaurant_id, "stock", title, detail=detail, severity=severity,
                                source_key=f"stock:{stamp}", meta={"items": low[:40]}, db_path=db_path)
    return issue if token else None


def auto_close(restaurant_id, db_path=DB_PATH):
    """Close the issues whose problem is gone, so nobody is chased about a
    thing already fixed (#18): a review issue once its reply has posted (or
    been approved to post). Stock closes in open_from_signals and coverage
    in the coverage check, where the facts that close them are read.
    Returns how many closed."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT o.id FROM ops_issues o JOIN reviews rv ON rv.restaurant_id=o.restaurant_id "
            "AND o.source_key='review:' || rv.id WHERE o.restaurant_id=? AND o.kind='review' "
            "AND o.status!='resolved' AND rv.response_status IN ('posted','approved')",
            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    for row in rows:
        _resolve(restaurant_id, row["id"], "Closed automatically: the reply to this review was posted.", db_path)
    return len(rows)


def resolve_coverage(restaurant_id, day_iso, arrived_keys, db_path=DB_PATH):
    """Close today's "hasn't clocked in" issues for the people who since
    have. `arrived_keys` are staff_settings.name_key()s of everyone clocked
    in (aliases already resolved). Returns the names closed."""
    if not arrived_keys:
        return []
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, source_key, title FROM ops_issues WHERE restaurant_id=? AND kind='coverage' "
                            "AND status!='resolved' AND source_key LIKE ?",
                            (restaurant_id, f"coverage:{day_iso}:%")).fetchall()
    finally:
        conn.close()
    import staff_settings as _ss
    closed = []
    for row in rows:
        who = row["source_key"].split(":", 2)[2]
        if _ss.name_key(who) in arrived_keys:
            _resolve(restaurant_id, row["id"], "Closed automatically: they clocked in.", db_path)
            closed.append(who)
    return closed


def open_from_checklists(restaurant_id, db_path=DB_PATH, now_local=None):
    """An opening checklist still unfinished an hour after opening becomes
    the manager's issue. Nothing watched these before."""
    from datetime import date as _date
    if "manager" not in get_routing(restaurant_id, db_path):
        return []
    # A restaurant on task sheets is watched by task_sheets.evaluate (critical
    # lines past due, unfinished sheets at shift end): this flat-list check
    # would report the same work twice.
    conn = get_conn(db_path)
    try:
        on_sheets = conn.execute("SELECT 1 FROM task_sheets WHERE restaurant_id=? AND active=1 LIMIT 1",
                                 (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if on_sheets:
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
