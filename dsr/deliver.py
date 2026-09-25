"""
dsr.deliver — telling the owners and managers that a night's report is ready
(docs/plans/DSR_ENGINE_PLAN.md §3, §8).

  on_terminal(restaurant, report_id)   the pipeline's hook, once a version
                                       reaches final or provisional
  release_held()                       the scheduled job: pushes held through
                                       quiet hours go out when they end

WHO (Will's rule): every login at the location whose DSR view
(dsr.access.view_for) is "owner" gets the Owner DSR, every "manager" login
the Manager DSR. A login with no console (employee, support) gets nothing;
Cavnar admin logins are never recipients. The group owner whose login lives
on another location of the same group is an owner here too, as for the
morning brief. The content is always dsr.access.render(report, user, ...) —
this module never re-derives a figure, so a manager's copy is exactly the
manager payload (no budget, loss lines or food cost unless granted).

WHEN: at the first terminal version. Email goes at once; push waits out the
restaurant's alert quiet hours (restaurants.alert_quiet_start / _end, read
on the restaurant's own clock exactly as models.is_in_quiet_hours reads
them) and is held until they end — nobody is woken at midnight. A failed
night notifies nobody here: the pipeline has already captured every error
behind it (ops.capture, job "dsr"), which is what reaches the admin console
and the failure digest.

HOW MANY: one notice per night, per person, per channel. A night that went
out PROVISIONAL gets exactly one "Updated" notice when a later version is
final; nothing else ever sends again (a re-run of a final night, a second
provisional version, a retry). A push still held when the final version
lands is not doubled up: the held one goes out rendered from the latest
version, and no "Updated" push follows it.

CLAIM, THEN SEND. Every notice is a dsr_deliveries row written BEFORE the
send, UNIQUE per (restaurant, night, user, channel, kind), so a retry, a
second process or a re-run of the night can never send twice. A process
killed between the claim and the send leaves the row "sending": that
person misses one notice rather than getting two — the same trade
ops.claim_period makes for every scheduled send.

A local backend never sends (scheduler.scheduling_allowed): it holds a copy
of real restaurants and production's Resend and APNs keys, and pressing
Close day on a laptop must not email Erik.
"""
import logging
import time as _clock
from datetime import date, datetime, time as _time, timedelta

import dsr
from dsr import store

log = logging.getLogger("dsr")

ALERT_TYPE = "dsr"                 # push.PRIORITY / NOTIFICATION_MODULE / alert_log
EMAIL_TYPE = "send_dsr_email"      # email_log

FIRST = "first"
UPDATED = "updated"
KINDS = (FIRST, UPDATED)

EMAIL = "email"
PUSH = "push"
HISTORY = "history"                # the restaurant's one bell row per night and kind (user_id 0)

SENDING = "sending"                # claimed; the send is under way (or its process died)
SENT = "sent"
HELD = "held"                      # a push waiting for quiet hours to end
FAILED = "failed"                  # attempted and refused (Resend said no, the push raised)
SKIPPED = "skipped"                # decided not to: suppressed address, no key, no device left
EXPIRED = "expired"                # a held push too old to be worth sending

MAX_NIGHT_AGE_DAYS = 3             # a re-run of an older night notifies nobody
HELD_MAX_HOURS = 24                # a held push older than this is expired, not sent
RELEASE_MAX_ROWS = 500
RELEASE_MAX_SECONDS = 120
PUSH_BODY_MAX = 178


def _db(db_path):
    import models
    return db_path or models.DB_PATH


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _allowed():
    """Only where the scheduler may run — see the module docstring."""
    import scheduler
    return scheduler.scheduling_allowed()


# ── who ─────────────────────────────────────────────────────────────────────

def recipients(restaurant_id, db_path=None):
    """Every active login that gets this restaurant's DSR, each with its
    grants here and its `view` ("owner" | "manager"). Reads the same rows
    morning_brief.recipients does (the location's logins plus the group
    owner's), without the brief's own preference: Will's rule is every
    owner and every manager."""
    from auth import _grants_for
    from permissions import CONSOLE_ROLES, normalize_role
    from dsr import access
    conn = store.get_conn(_db(db_path))
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, role, is_admin, email FROM users WHERE restaurant_id=? AND COALESCE(is_active,1)=1 "
            "ORDER BY id", (restaurant_id,)).fetchall()]
        rows += [dict(r) for r in conn.execute(
            "SELECT u.id, u.role, u.is_admin, u.email FROM users u "
            "JOIN restaurants b ON b.id=u.restaurant_id JOIN restaurants r ON r.id=? "
            "WHERE u.role='owner' AND COALESCE(u.is_active,1)=1 AND b.location_group IS NOT NULL "
            "AND b.location_group=r.location_group AND b.owner_email=r.owner_email AND b.id != r.id "
            "ORDER BY u.id", (restaurant_id,)).fetchall()]
        out, seen = [], set()
        for u in rows:
            if u["id"] in seen or u["is_admin"] or normalize_role(u["role"]) not in CONSOLE_ROLES:
                continue
            seen.add(u["id"])
            u["grants"] = _grants_for(conn, u["id"], restaurant_id)
            u["restaurant_id"] = restaurant_id
            u["view"] = access.view_for(u)
            if u["view"] in (access.OWNER, access.MANAGER):
                out.append(u)
    finally:
        conn.close()
    return out


def _devices(restaurant_id, db):
    """{user_id: [device rows]} for the phones a push here would reach."""
    import push
    out = {}
    for t in (push.get_device_tokens(restaurant_id, db, for_delivery=True) or []):
        out.setdefault(int(t.get("user_id") or 0), []).append(t)
    return out


# ── quiet hours ─────────────────────────────────────────────────────────────

def _minutes(value):
    h, m = str(value).strip().split(":")[:2]
    return int(h) * 60 + int(m)


def quiet_until(restaurant, now_utc):
    """When the restaurant's alert quiet hours end (naive UTC), or None when
    `now_utc` is outside them. The same window models.is_in_quiet_hours
    reads — the restaurant's own clock, a start after the end crossing
    midnight, an equal start and end never quiet — but for a given moment,
    so a held push knows when it may go."""
    start = getattr(restaurant, "alert_quiet_start", None)
    end = getattr(restaurant, "alert_quiet_end", None)
    if not start or not end:
        return None
    try:
        s, e = _minutes(start), _minutes(end)
    except (TypeError, ValueError):
        return None
    from dsr.pipeline import local_time, to_utc
    local = local_time(restaurant, now_utc)
    n = local.hour * 60 + local.minute
    inside = (s <= n < e) if s <= e else (n >= s or n < e)
    if not inside:
        return None
    ends = datetime.combine(local.date(), _time(e // 60, e % 60))
    if ends <= local:
        ends += timedelta(days=1)
    return to_utc(restaurant, ends)


# ── the claim ───────────────────────────────────────────────────────────────

def _rows(db, rid, day):
    """{(user_id, channel, kind): row} already written for the night."""
    conn = store.get_conn(db)
    try:
        rows = conn.execute("SELECT * FROM dsr_deliveries WHERE restaurant_id=? AND business_date=?",
                            (rid, day)).fetchall()
    finally:
        conn.close()
    return {(r["user_id"], r["channel"], r["kind"]): dict(r) for r in rows}


def _claim(db, rid, day, user_id, channel, kind, report, view, status, hold_until=None):
    """Write the row that says this notice is being sent. Returns its id, or
    None when someone already has it (the UNIQUE constraint is the claim)."""
    conn = store.get_conn(db)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO dsr_deliveries (restaurant_id, business_date, user_id, channel, kind, "
            "report_id, version, provisional, view, status, hold_until) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, day, int(user_id), channel, kind, report["id"], report["version"],
             1 if report.get("provisional") else 0, view, status, _stamp(hold_until)))
        conn.commit()
        return cur.lastrowid if cur.rowcount == 1 else None
    finally:
        conn.close()


def _finish(db, row_id, status, detail=None, report=None, hold_until=None):
    fields, args = ["status=?", "detail=?"], [status, (str(detail)[:300] if detail else None)]
    if status == SENT:
        fields.append("sent_at=datetime('now')")
    if report is not None:
        fields += ["report_id=?", "version=?", "provisional=?"]
        args += [report["id"], report["version"], 1 if report.get("provisional") else 0]
    if status == HELD:
        fields.append("hold_until=?")
        args.append(_stamp(hold_until))
    conn = store.get_conn(db)
    try:
        conn.execute(f"UPDATE dsr_deliveries SET {', '.join(fields)} WHERE id=?", (*args, row_id))
        conn.commit()
    finally:
        conn.close()


def _kind(first, updated, report):
    """Which notice this version owes a person on one channel, or None.

    Nothing yet → the first. A provisional version never sends a second
    time. A final version after a provisional first → the one update, once."""
    if first is None:
        return FIRST
    if report.get("provisional"):
        return None
    if first.get("provisional") and updated is None:
        return UPDATED
    return None


# A first notice in one of these never reached the person (D2-10): Resend
# refused it, it was skipped, or it was held too long and expired. SENDING
# (a process that died mid-send) may have gone out, and HELD still will.
NEVER_ARRIVED = (FAILED, SKIPPED, EXPIRED)


def _content(kind, first):
    """What a notice owed under `kind` SAYS: an "Updated" notice to someone
    whose first notice never arrived is the first notice's content — they
    never read "went out provisional", so it is not an update to them."""
    if kind == UPDATED and first is not None and first.get("status") in NEVER_ARRIVED:
        return FIRST
    return kind


# ── what it says ────────────────────────────────────────────────────────────

def report_url(business_date, rid=None):
    """The web report for the night — the hash route the web client opens.
    `rid` names the location: a group owner's session may sit on a sibling
    location, and the #dsr route switches to this one first (D2-7)."""
    import config
    q = ""
    if rid:
        try:
            q = f"?rid={int(rid)}"
        except (TypeError, ValueError):
            q = ""
    return f"{config.base_url()}/{q}#dsr/{str(business_date)[:10]}"


def _money(v):
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _pct(v):
    return f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.1f}%"


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _text(item):
    if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
        return item["text"].strip()
    return None


def _first_sentence(text):
    import re
    if not text:
        return None
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip())
    return (m.group(1) if m else text.strip())


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


def digest(payload, restaurant, kind=FIRST):
    """What an email or a push says about one rendered DSR (the output of
    dsr.access.render for ONE login) — every figure read from that payload,
    none computed here beyond formatting. A metric the payload does not
    carry (withheld from this view, or never measured) is left out, never
    shown as zero."""
    from dsr import access
    day = date.fromisoformat(str(payload.get("business_date"))[:10])
    from time_utils import mdy
    facts = payload.get("facts") or {}
    blocks = facts.get("blocks") or {}
    sales = blocks.get("sales") or {}
    sm = (sales.get("metrics") or {}) if sales.get("status") == dsr.READY else {}
    labor = blocks.get("labor") or {}
    lm = (labor.get("metrics") or {}) if labor.get("status") == dsr.READY else {}
    wd = day.strftime("%a")

    net = sm.get("net") if _num(sm.get("net")) else None
    stats, compare = [], None
    if net is not None:
        stats.append({"value": _money(net), "label": "Net sales", "tone": None})
    for key, label in (("vs_last_week_pct", f"vs last {wd}"), ("vs_last_year_pct", "vs last year"),
                       ("vs_budget_net_pct", "vs budget")):
        v = sm.get(key)
        if _num(v):
            stats.append({"value": _pct(v), "label": label, "tone": "good" if v > 0 else "bad" if v < 0 else None})
    if _num(sm.get("vs_last_week_pct")):
        v = sm["vs_last_week_pct"]
        compare = (f"{abs(v):.1f}% {'above' if v > 0 else 'below'} last {wd}" if v else f"even with last {wd}")
    if _num(lm.get("pct")):
        target = lm.get("target_pct") if _num(lm.get("target_pct")) else None
        pts = lm.get("vs_target_pts") if _num(lm.get("vs_target_pts")) else None
        # A starting target the owner never set is never "over" in red
        # (thresholds.target_for; the scorecard and the web tile agree).
        starting = (labor.get("detail") or {}).get("target_source") == "default"
        stats.append({"value": f"{lm['pct']:.1f}%",
                      "label": "Labor" + (f" · target {target:g}%" if target is not None else ""),
                      "tone": None if pts is None else (("warn" if starting else "bad") if pts > 0 else "good")})

    narrative = payload.get("narrative") or {}
    notes = (payload.get("checklist") or {}).get("narrative") or {}
    lead = _text(narrative.get("executive_summary"))
    lead_missing = None
    if lead is None and not narrative and notes.get("reason"):
        lead_missing = str(notes["reason"])
    elif lead is None and narrative:
        # A summary was written but none of it is this view's: say so, as
        # the app does, rather than silently leaving the opening out (D2-4).
        lead_missing = access.NO_LEAD_FOR_VIEW
    went = [t for t in (_text(x) for x in narrative.get("went_well") or []) if t]
    needs = [t for t in (_text(x) for x in narrative.get("needs_attention") or []) if t]
    # `key` is the action's rec_ledger key (dsr_action:…): the email's
    # "Ask about this" link names it, and a delivered email presents it.
    # Each action keeps its measured confidence (K1, dsr.narrative.
    # action_confidence) and the one line the email prints for it (T1):
    # the app showed "72%" and the email the same advice with nothing.
    import rec_trust
    actions = [{"text": _text(a), "why": (a.get("why") or "").strip() or None,
                "key": a.get("key") if isinstance(a.get("key"), str) else None,
                "confidence": a.get("confidence") if isinstance(a.get("confidence"), dict) else None,
                "confidence_label": rec_trust.outbound_label(a.get("confidence")) or None}
               for a in narrative.get("actions_tomorrow") or [] if _text(a)]
    withheld = [access.BLOCK_LABELS.get(n, n) for n in facts.get("withheld") or []]
    fiscal = (payload.get("fiscal") or {}).get("label")
    return {
        "name": (getattr(restaurant, "location_name", None) or getattr(restaurant, "name", None) or "Your restaurant"),
        "business_date": day.isoformat(),
        "date_short": f"{wd} {mdy(day)}",
        "date_long": f"{day.strftime('%A')} {mdy(day)}",
        "weekday": day.strftime("%A"),
        "fiscal": fiscal,
        "view": payload.get("view"),
        "kind": kind,
        "provisional": bool(payload.get("provisional")),
        "version": payload.get("version"),
        "net": net,
        "net_label": _money(net) if net is not None else None,
        "compare": compare,
        "stats": stats,
        "lead": lead,
        "lead_missing": lead_missing,
        "went_well": went,
        "needs_attention": needs,
        # The owner's "did we win today?" (dsr.scorecard) — None for the
        # manager's view; the email and the push lead with it when present.
        "scorecard": payload.get("scorecard") if isinstance(payload.get("scorecard"), dict) else None,
        # Both views (9/25/26): the numbers with direction, the manager's
        # shift and operations, tomorrow, and yesterday's predictions graded.
        "kpis": list(payload.get("kpis") or []),
        # The keys of the few KPIs the report leads with (kpis.headline): the
        # email prints only these; None when the payload predates them.
        "kpis_headline": (list(payload["kpis_headline"]) if isinstance(payload.get("kpis_headline"), list)
                          else None),
        "shift": payload.get("shift"),
        "operations": list(payload.get("operations") or []),
        "tomorrow": payload.get("tomorrow"),
        "yesterday": payload.get("yesterday"),
        # AI insights (access.insights, already filtered for this view and
        # capped at access.INSIGHTS_MAX): the email shows them after the KPIs.
        "insights": [i for i in payload.get("insights") or [] if isinstance(i, dict) and i.get("text")],
        "actions": actions,
        "missing": list(facts.get("missing") or []),
        "withheld": withheld,
        "url": report_url(day, getattr(restaurant, "id", None)),
        "restaurant_id": getattr(restaurant, "id", None),
    }


def push_text(d):
    """(title, body) for the push. The owner's and the manager's differ
    only in whose report it is and which line leads."""
    from dsr import access
    whose = "Your daily report" if d.get("view") == access.OWNER else "Your manager report"
    head = f"{d['name']} · {d['date_short']}"
    if d["kind"] == UPDATED:
        if d.get("net_label"):
            return "Sales are now in — updated report", _clip(f"{head}: {d['net_label']} net.", PUSH_BODY_MAX)
        # Final without a net: the POS closed the day with no sales in it
        # (pipeline D1-20) — never "sales are now in".
        return f"{whose} is final", _clip(f"{head}: no sales were recorded for the night.", PUSH_BODY_MAX)
    if d.get("provisional"):
        return (f"{whose} is ready — provisional",
                _clip(f"{head}: sales are still syncing. You'll get one update when they land.", PUSH_BODY_MAX))
    card = d.get("scorecard") or {}
    if card.get("verdict") and card.get("overall") is not None:
        sales = next((c for c in card.get("components") or [] if c.get("key") == "sales" and c.get("measured")), None)
        # The first risk that isn't the sales line the body already carries.
        risk = next((x for x in card.get("risks") or [] if x.get("key") != "sales_budget"), None)
        figs = ", ".join(x for x in (f"{d['net_label']} net" if d.get("net_label") else None,
                                     sales.get("value") if sales else None) if x)
        body = f"{head}: {card['verdict']['label']}, {card['overall']}/100" + (f" · {figs}." if figs else ".")
        if risk and risk.get("text"):
            body += f" Watch: {risk['text']}."
        return f"{whose} is ready", _clip(body, PUSH_BODY_MAX)
    figures = ", ".join(x for x in (f"{d['net_label']} net" if d.get("net_label") else None, d.get("compare")) if x)
    # The first thing that needs attention (else went well) — the lead would
    # only repeat the net the body already opens with.
    line = next(iter(d.get("needs_attention") or d.get("went_well") or []), None) or d.get("lead")
    body = f"{head}: {figures}." if figures else f"{head}."
    if line:
        body += " " + _first_sentence(line)
    return f"{whose} is ready", _clip(body, PUSH_BODY_MAX)


# ── sending ─────────────────────────────────────────────────────────────────

def _render(report, user, restaurant, db):
    from dsr import access
    versions = store.versions(report["restaurant_id"], report["business_date"], db_path=db)
    return access.render(report, user, restaurant, versions=versions)


def present_shown(restaurant_id, payload, surface, user_id=None, db_path=None, limit=None):
    """The actions a reader was shown in one rendered report (the output of
    dsr.access.render for THEIR view), into rec_ledger on `surface`. Returns
    {key: rec_id or None}; never raises. Called where the report is really
    seen — a delivered email ("dsr_email"), the report view ("dsr") — never
    when the narrative is written. `limit`: only the first N were shown (the
    email prints emails.DSR_EMAIL_ACTIONS of them)."""
    try:
        from dsr import narrative as _narr
        import rec_ledger
        acts = ((payload or {}).get("narrative") or {}).get("actions_tomorrow") or []
        if limit is not None:
            acts = list(acts)[:limit]
        items = _narr.ledger_items(acts)
        if not items:
            return {}
        return rec_ledger.present_many(restaurant_id, items, surface, user_id=user_id, db_path=_db(db_path))
    except Exception as e:
        log.warning("dsr: actions not presented rid=%s surface=%s: %s", restaurant_id, surface, e)
        return {}


def _send_email(db, row_id, restaurant, report, user, kind, content=None):
    """`kind` is the claim; `content` what the email says (_content)."""
    import emails
    content = content or kind
    payload = _render(report, user, restaurant, db)
    d = digest(payload, restaurant, content)
    result = emails.send_dsr_email(user["email"], d, restaurant_id=restaurant.id)
    if getattr(result, "ok", False):
        _finish(db, row_id, SENT)
        # The "Updated" notice shows no actions; the first notice does.
        if content == FIRST:
            present_shown(restaurant.id, payload, "dsr_email", user_id=user.get("id"), db_path=db,
                          limit=emails.DSR_EMAIL_ACTIONS)
        return SENT
    # Never attempted (a suppressed address, no key): a decision, not a failure.
    status = FAILED if getattr(result, "attempts", 0) else SKIPPED
    _finish(db, row_id, status, detail=getattr(result, "error", None))
    return status


def _history(db, restaurant, day, kind, report):
    """The restaurant's one notification-history row for the night and kind
    (the bell, and the id a push open reports) — written once, however many
    phones the notice reaches. Returns the alert_log id, or None."""
    row_id = _claim(db, restaurant.id, day, 0, HISTORY, kind, report, None, SENDING)
    if row_id is None:
        row = _rows(db, restaurant.id, day).get((0, HISTORY, kind)) or {}
        try:
            return int(row.get("detail")) if row.get("detail") else None
        except (TypeError, ValueError):
            return None
    import notify
    alert_id = notify.record_notification(restaurant.id, ALERT_TYPE, db_path=db)
    _finish(db, row_id, SENT, detail=alert_id)
    return alert_id


def push_data(business_date, kind, version, alert_id=None):
    """The push's `cavnar` payload. The app opens the report from exactly
    {"type": "dsr", "business_date": "YYYY-MM-DD"}; the collapse key keeps
    the update and the first notice as one row on the lock screen."""
    data = {"type": ALERT_TYPE, "business_date": str(business_date)[:10], "kind": kind, "version": version,
            "collapse_key": f"dsr-{str(business_date)[:10]}"}
    if alert_id:
        data["alert_id"] = alert_id
    return data


def _send_push(db, row_id, restaurant, report, user, kind, content=None):
    """`kind` is the claim (and the bell row); `content` what it says."""
    import push
    d = digest(_render(report, user, restaurant, db), restaurant, content or kind)
    title, body = push_text(d)
    alert_id = _history(db, restaurant, report["business_date"], kind, report)
    push.fire_push(restaurant.id, ALERT_TYPE, title, body,
                   data=push_data(report["business_date"], kind, report["version"], alert_id),
                   db_path=db, user_ids={user["id"]})
    _finish(db, row_id, SENT, report=report)
    return SENT


def on_terminal(restaurant, report_id, now_utc=None, db_path=None):
    """The pipeline's hook: a version just reached final or provisional.
    Claims and sends whatever each recipient is owed. Never raises for one
    person's failure (captured, and the rest still go); the caller wraps the
    whole call so delivery can never fail the report."""
    import ops
    db = _db(db_path)
    now_utc = now_utc or datetime.utcnow()
    out = {"email": 0, "push": 0, "held": 0, "skipped": 0, "failed": 0}
    report = store.get_report_by_id(report_id, restaurant_id=restaurant.id, db_path=db)
    if not report or report["status"] not in ("final", "provisional"):
        return dict(out, reason="not a deliverable version")
    if not _allowed():
        return dict(out, reason="not the production scheduler host")
    if not getattr(restaurant, "dsr_notify", 0):
        # Off until the owner turns it on (Account → Daily report): the
        # report still builds and shows in the app, nobody is emailed.
        return dict(out, reason="delivery is off for this restaurant")
    from dsr.pipeline import local_time
    day = report["business_date"]
    if (local_time(restaurant, now_utc).date() - date.fromisoformat(day)).days > MAX_NIGHT_AGE_DAYS:
        return dict(out, reason="too old a night to announce")
    people = recipients(restaurant.id, db)
    if not people:
        return dict(out, reason="nobody to tell")
    done = _rows(db, restaurant.id, day)
    devices = _devices(restaurant.id, db)
    held_until = quiet_until(restaurant, now_utc)
    for u in people:
        for channel in (EMAIL, PUSH):
            try:
                if channel == EMAIL and not u.get("email"):
                    continue
                if channel == PUSH and not devices.get(u["id"]):
                    continue
                first = done.get((u["id"], channel, FIRST))
                kind = _kind(first, done.get((u["id"], channel, UPDATED)), report)
                if kind is None:
                    continue
                if channel == PUSH and kind == UPDATED and first and first["status"] == HELD:
                    # The held first push goes out from the latest version;
                    # an "Updated" behind it would be a second push.
                    continue
                if channel == PUSH and held_until is not None:
                    if _claim(db, restaurant.id, day, u["id"], PUSH, kind, report, u["view"], HELD,
                              hold_until=held_until):
                        out["held"] += 1
                    continue
                row_id = _claim(db, restaurant.id, day, u["id"], channel, kind, report, u["view"], SENDING)
                if row_id is None:
                    continue
                try:
                    status = (_send_email if channel == EMAIL else _send_push)(db, row_id, restaurant, report, u, kind,
                                                                                content=_content(kind, first))
                except Exception as e:
                    _finish(db, row_id, FAILED, detail=e)
                    raise
                if status == SENT:
                    out[channel] += 1
                else:
                    out["skipped" if status == SKIPPED else "failed"] += 1
            except Exception as e:
                out["failed"] += 1
                ops.capture(e, job="dsr_deliver",
                            context=f"restaurant_id={restaurant.id} business_date={day} user_id={u['id']} {channel}")
    return out


# ── the held pushes ─────────────────────────────────────────────────────────

def _latest_deliverable(rid, day, db):
    """The newest final or provisional version of the night — a failed
    re-run never replaces what was already reported."""
    conn = store.get_conn(db)
    try:
        row = conn.execute("SELECT id FROM dsr_reports WHERE restaurant_id=? AND business_date=? "
                           "AND status IN ('final','provisional') ORDER BY version DESC LIMIT 1",
                           (rid, day)).fetchone()
    finally:
        conn.close()
    return store.get_report_by_id(row["id"], db_path=db) if row else None


def _take(db, row_id):
    """Move one held row to sending — the claim a release makes, so two
    passes never send the same held push."""
    conn = store.get_conn(db)
    try:
        cur = conn.execute("UPDATE dsr_deliveries SET status=? WHERE id=? AND status=?", (SENDING, row_id, HELD))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _release_one(row, now_utc, db):
    from models import get_restaurant
    r = get_restaurant(row["restaurant_id"], db_path=db)
    created = datetime.fromisoformat(str(row["created_at"])[:19].replace(" ", "T"))
    if now_utc - created > timedelta(hours=HELD_MAX_HOURS):
        _finish(db, row["id"], EXPIRED, detail="held too long to be worth sending")
        return EXPIRED
    if r is None or not getattr(r, "dsr_enabled", 1) or not getattr(r, "dsr_notify", 0) or \
            (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled", "paused"):
        # Notices switched off while the push was held (D2-9) is as final as
        # the DSR switched off: on_terminal would not have sent it either.
        _finish(db, row["id"], SKIPPED, detail="restaurant no longer receives the DSR")
        return SKIPPED
    again = quiet_until(r, now_utc)
    if again is not None:
        # The quiet window moved (the owner changed it overnight).
        _finish(db, row["id"], HELD, hold_until=again)
        return HELD
    user = next((u for u in recipients(r.id, db) if u["id"] == row["user_id"]), None)
    if user is None:
        _finish(db, row["id"], SKIPPED, detail="no longer a recipient")
        return SKIPPED
    if not _devices(r.id, db).get(user["id"]):
        _finish(db, row["id"], SKIPPED, detail="no device left to push to")
        return SKIPPED
    report = _latest_deliverable(r.id, row["business_date"], db)
    if report is None or (row["kind"] == UPDATED and report.get("provisional")):
        _finish(db, row["id"], SKIPPED, detail="no deliverable version")
        return SKIPPED
    first = _rows(db, r.id, row["business_date"]).get((user["id"], PUSH, FIRST)) if row["kind"] == UPDATED else None
    return _send_push(db, row["id"], r, report, user, row["kind"], content=_content(row["kind"], first))


def release_held(now_utc=None, db_path=None):
    """Scheduler entry (every tick, claimed per 10-minute slot): send the
    pushes whose quiet hours are over. Bounded by RELEASE_MAX_ROWS and
    RELEASE_MAX_SECONDS; the held rows are the queue, oldest release time
    first, so a pass that stops early leaves the rest held for the next."""
    import ops
    db = _db(db_path)
    now_utc = now_utc or datetime.utcnow()
    if not _allowed():
        return {"released": 0, "reason": "not the production scheduler host"}
    conn = store.get_conn(db)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM dsr_deliveries WHERE channel=? AND status=? AND hold_until <= ? "
            "ORDER BY hold_until, id LIMIT ?", (PUSH, HELD, _stamp(now_utc), RELEASE_MAX_ROWS)).fetchall()]
    finally:
        conn.close()
    started = _clock.monotonic()
    counts, hit_bound = {}, False
    for row in rows:
        if _clock.monotonic() - started > RELEASE_MAX_SECONDS:
            hit_bound = True
            break
        if not _take(db, row["id"]):
            continue
        try:
            status = _release_one(row, now_utc, db)
        except Exception as e:
            _finish(db, row["id"], FAILED, detail=e)
            ops.capture(e, job="dsr_delivery",
                        context=f"restaurant_id={row['restaurant_id']} business_date={row['business_date']} "
                                f"user_id={row['user_id']}")
            status = FAILED
        counts[status] = counts.get(status, 0) + 1
    return {"due": len(rows), "released": counts.get(SENT, 0), "counts": counts, "hit_bound": hit_bound}


# ── the closing summary it replaces ─────────────────────────────────────────

def replaces_closing_summary(restaurant):
    """True when this restaurant's night is announced by the DSR, so the old
    "How tonight went" push (strategy_jobs.run_closing_summary) stays quiet:
    the DSR is on and a POS is connected — exactly the restaurants the DSR
    sweep runs for (dsr.pipeline.run_sweep). Every other restaurant keeps
    the closing summary. Fails toward keeping it."""
    if not getattr(restaurant, "dsr_enabled", 1) or not getattr(restaurant, "dsr_notify", 0):
        # With delivery off nothing announces the night, so the old push stays.
        return False
    try:
        import pos
        return bool(pos.connected_provider(restaurant.id)[0])
    except Exception as e:
        log.warning("dsr: POS check for the closing summary failed rid=%s: %s", getattr(restaurant, "id", None), e)
        return False
