"""
schedule_publish.py — sending a week to staff (schedule audit 10/10/26
ARCH-15, ARCH-19, ARCH-20, ARCH-21, BUGSLIFE-2, -6, -7, -9, -10, -11).

The publish state machine used to live in the route module (client_api),
imported lazily by the delayed run, the auto-publish job and the engine; it
told every member of staff synchronously inside the HTTP request and only
then stamped the week, so a stamp that failed after the messages went out
was "captured" and reported as success, and a week that a generation
superseded mid-send left staff told "posted" about a week no reader could
find. Here, with no Flask import, routes and jobs call one service:

1. the gate (`publish_review`) — the rule sweep against today's data;
2. the claim — one Send at a time per week (`publishing_at`);
3. the STAMP, in one write transaction: the copy still in force and still
   the rows the sender reviewed (`version`, BUGSLIFE-11) and the gate read
   (ARCH-21, BUGSLIFE-6); published_at and the PUBLISHED copy
   (published_csv) set from exactly those rows; any older published copy of
   the week retired; the `published` version appended; and one outbox row
   per person to tell (`schedule_notices`) — everyone on a first send; on a
   replacement or a changes send only the people whose shifts differ, the
   people taken off the week included (BUGSLIFE-7);
4. DELIVERY from the outbox, after the commit: inline for at most
   INLINE_SECONDS, the rest by the `schedule_notices` job; each person's
   result is kept (sent / unreachable / failed), a failure is retried with
   backoff and never counted as told, and "Send again" re-queues the people
   who have not got it — or everyone, when asked (BUGSLIFE-9).

client_api re-exports the old names (`publish_review`, `publish_blockers`,
`_publish_schedule`, `send_schedule_changes`, ...) so callers and tests keep
working.
"""
import json
import logging
import time as _time
import uuid as _uuid
from datetime import datetime as _dt, timedelta as _td

import config
import models as _models_mod
import ops as _ops

log = logging.getLogger(__name__)

# A Send delivers inline for at most this long, so a publish to a large
# staff on email or text never holds a request thread for minutes (ARCH-20);
# the rest go out from the outbox job within the minute.
INLINE_SECONDS = 8.0
# A failed notice is tried again after these minutes, then left failed (the
# owner's "Send again" re-queues it).
NOTICE_RETRY_MINUTES = (5, 30, 120)
NOTICE_MAX_ATTEMPTS = len(NOTICE_RETRY_MINUTES) + 1
# A notice claimed for delivery longer ago than this belongs to a delivery
# that died; it is tried again.
NOTICE_LEASE_MINUTES = 10
# The outbox job's own bound per pass, and the bound when it runs as one of
# the scheduler's minute duties (scheduler._minute_duties).
NOTICE_JOB_SECONDS = 45.0
NOTICE_DUTY_SECONDS = 20.0

NOTICE_KINDS = ("posted", "changed", "removed")

# How many times a Send with no reviewed version re-checks a week a save
# changed while its gate ran, before it gives up and asks for a press again.
GATE_RETRIES = 2


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def get_restaurant(restaurant_id):
    return _models_mod.get_restaurant(restaurant_id)


def log_account_event(restaurant_id, event_type, current_user=None, detail=None, extra=None):
    """Account activity log — models.log_account_event, resolved at call
    time; the row client_api.log_account_event writes, without the route
    module."""
    _models_mod.log_account_event(restaurant_id, event_type, current_user=current_user, detail=detail, extra=extra)


def init_schedule_notices(db_path=None):
    """The publish outbox (boot, from models.init_db): one row per person a
    Send, a changes send or a resend tells. A row is written in the same
    transaction as the stamp, delivered after it, and keeps what happened —
    sent (on which channel), unreachable (nothing on file), failed (retried
    at next_attempt_at until NOTICE_MAX_ATTEMPTS) or cancelled (a newer
    notice about the same week replaced it before it went)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_notices (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL,
            history_id       INTEGER NOT NULL,
            week_start       TEXT,
            batch            TEXT    NOT NULL,      -- one Send / changes send / resend
            employee_name    TEXT    NOT NULL,
            person_id        INTEGER,
            kind             TEXT    NOT NULL,      -- posted | changed | removed
            status           TEXT    NOT NULL DEFAULT 'pending',  -- pending | sending | sent | unreachable | failed | cancelled
            channel          TEXT,                  -- push | sms | email
            sent_to          TEXT,
            delivery         TEXT,                  -- pending (push) | held_until_morning (text)
            shifts           INTEGER,
            attempts         INTEGER NOT NULL DEFAULT 0,
            last_error       TEXT,
            next_attempt_at  TEXT,
            claimed_at       TEXT,
            requested_by     TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            sent_at          TEXT,
            UNIQUE(batch, employee_name)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_notices_due ON schedule_notices(status, next_attempt_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_notices_week ON schedule_notices(restaurant_id, history_id)")
        # The retention prune's column (ops._RETENTION_COLUMN).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_notices_created ON schedule_notices(created_at)")
        conn.commit()
    finally:
        conn.close()


def _utc(at=None) -> str:
    return (at or _dt.utcnow()).strftime("%Y-%m-%d %H:%M:%S")


def _fold(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def publish_blockers(restaurant_id, schedule_id=None, unattended=False):
    """Why this schedule should not go to staff unread: rows the engine
    flagged (hard rule breaches, names it could not vouch for), and a week
    the quality engine judged weak or could not judge. Empty means clear.
    The texts of publish_review's blockers — see there."""
    return [b["text"] for b in publish_review(restaurant_id, schedule_id, unattended=unattended)["blockers"]]


def _local_today(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        from datetime import date as _d
        return _d.today()


# What each source the rules could not read means for the week, in the
# owner's words (schedule_rules._input_problem — schedule audit 10/3/26 P-1):
# one wording, schedule_rules.INPUT_PROBLEM_WORDS, shared with the
# generation's review.
def _input_problem_text(p) -> str:
    """One blocker line for a setting or source the rules could not read
    (Constraints.input_problems) — schedule_rules.input_problem_text, the
    line the generation's review names it with."""
    import schedule_rules as _sr_ipt
    return _sr_ipt.input_problem_text(p)


def _notice_text(short, week_start) -> str:
    """The notice-rule blocker. A week already under way said "starts in -2
    days" (schedule audit 10/3/26 E-22): it says when it started."""
    from time_utils import mdy as _mdy_n
    given = int(short["days_given"])
    when = (f"starts in {given} day{'s' if given != 1 else ''}" if given > 0 else "starts today" if given == 0
            else f"started {-given} day{'s' if given != -1 else ''} ago")
    return (f"Less notice than your {short['notice_days']}-day schedule notice rule: the week of "
            f"{_mdy_n(week_start)} {when}")


def _live_week(restaurant_id, week_start, not_id):
    """The copy of `week_start` staff have now — its live copy
    (models.live_week_row), with its PUBLISHED rows as `schedule_csv` —
    when that is another row than `not_id`; else None."""
    conn = get_conn()
    try:
        live = _models_mod.live_week_row(conn, restaurant_id, week_start)
    finally:
        conn.close()
    if live is None or live["id"] == not_id or not live["published_at"]:
        return None
    return live


def _in_restaurant_dayparts(fn):
    """schedule_engine.in_restaurant_dayparts, read at call time (the gate
    runs inside the restaurant's own lunch/dinner boundary, ARCH-12)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        import schedule_engine as _se_dp
        return _se_dp.in_restaurant_dayparts(fn)(*a, **k)
    return wrapper


@_in_restaurant_dayparts
def publish_review(restaurant_id, schedule_id=None, unattended=False, today=None) -> dict:
    """{"blockers": [{"key", "text"}], "soft": [text], "notes": [{"key",
    "text"}], "hours": {"hourly", "salaried", "total"}, "schedule_id"} — the
    publish gate, computed against the data as it stands NOW.

    It read the review saved when the draft was generated, so time off
    approved afterwards never reached it: a week with a fresh hard breach
    went to staff by a manual, delayed or automatic publish alike (NS5 H3).
    The rule sweep is re-run here on every call, over the saved review's
    lines (a line from either is a blocker), and the notice rule is checked
    against today's date (NS5 H2 notice_short).

    Blockers are what must be read before the week goes out: hard rule
    breaches, a setting or source the rules could not read (P-1), the rule
    check or the quality engine failing to run (P-16), the notice rule, a
    change inside the notice window to a week staff already have (E-22),
    and hourly hours past the hourly budget (E-7). The quality verdict —
    a weak week, low confidence, shifts below the bar — is a note, never a
    blocker (SQ-29): the scorer's structural caps put one on nearly every
    week, which taught owners to acknowledge without reading. Notes ride in
    `notes` (with keys) and `soft`.

    `unattended` is automation (auto-publish, its delayed run): the soft
    flags schedule_rules.HOLD_UNATTENDED names stop it too, and every other
    soft flag is returned in `soft` for the owner's notice (NS5 M7). Each
    blocker carries a stable `key` so an acknowledgement given for one set
    does not carry to a blocker that appeared later."""
    _ensure_history_columns = _models_mod._ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        cols = ("SELECT id, week_start, week_end, schedule_csv, review_json, quality_json, hours_scheduled, "
                "hours_hourly, hours_salaried, hours_budget, economics_json, published_at FROM schedule_history ")
        if schedule_id:
            row = conn.execute(cols + "WHERE id=? AND restaurant_id=?", (int(schedule_id), restaurant_id)).fetchone()
        else:
            # The week in force (models.newest_draft), never simply the
            # newest row — a discarded rewrite or a replaced draft (ARCH-11).
            row = conn.execute(cols + "WHERE restaurant_id=? AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
                               (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"blockers": [], "soft": [], "notes": [], "hours": None, "schedule_id": None}
    out, soft, notes, seen = [], [], [], set()
    pp_out = None          # the priced late changes (predictability_pay.preview), when a law covers it

    def add(key, text):
        if text and text not in seen:
            seen.add(text)
            out.append({"key": key, "text": text})

    def note(key, text):
        # Acknowledge-able, never holding a publish (SQ-29).
        if text and text not in seen:
            seen.add(text)
            notes.append({"key": key, "text": text})
            soft.append(text)

    flagged = sum(1 for line in (row["schedule_csv"] or "").split("\n") if "NEEDS REVIEW" in line)
    if flagged:
        add("needs_review", f"{flagged} shift{'s' if flagged != 1 else ''} marked NEEDS REVIEW")
    try:
        review = json.loads(row["review_json"] or "null") or {}
    except Exception:
        review = {}
    # The generation's own lead lines are read whole (re-audit 10/4/26
    # PIPE-7): with three budget lines and a caveat ahead of it, the
    # unwritten-day line sat at the edge of the first six.
    _lead = [x for x in (review.get("lead_lines") or []) if isinstance(x, str)]
    for line in _lead + [x for x in (review.get("lines") or [])[:6] if isinstance(x, str)]:
        if line.startswith("⚠") and "over the ceiling" not in line:   # the hours check below says it once
            t = line.lstrip("⚠ ").strip()
            add("saved:" + t, t)
    # A repair stage of a hard rule or the manager rule that did not run on
    # this draft (schedule audit 10/3/26 P-3, P-17): its breaches were never
    # repaired, so the week is read before it goes out — wherever its line
    # sits in the review.
    for f in review.get("stage_failures") or []:
        if f.get("blocks_publish"):
            add(f"stage:{f.get('stage')}", (str(f.get("line") or "").lstrip("⚠ ").strip()
                                            or f"A repair step ({f.get('stage')}) didn't run on this draft"))
    # The sweep against today's data: time off approved, a person
    # deactivated, a floor or a minor's age band set since the draft.
    import schedule_rules as _sr
    from schedule_versions import rows_from_csv
    rows = rows_from_csv(row["schedule_csv"] or "")
    c = None
    try:
        from datetime import datetime as _dt, timedelta as _td
        ws = _dt.strptime(str(row["week_start"])[:10], "%Y-%m-%d")
        we = _dt.strptime(str(row["week_end"] or row["week_start"])[:10], "%Y-%m-%d")
        dates = [(ws + _td(days=i)).strftime("%Y-%m-%d") for i in range(max(0, min(13, (we - ws).days)) + 1)]
        days = [_dt.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates]
        c = _sr.build_constraints(restaurant_id, dates, days, restaurant=get_restaurant(restaurant_id))
        # A setting or source the rules could not read checked this week
        # without it — a person's managers, minors, hours limits or
        # certificates silently gone (P-1). Named, never passed over.
        for p in (getattr(c, "input_problems", None) or []):
            add(f"input:{p.get('source')}:{(p.get('name') or '').strip().lower()}", _input_problem_text(p))
        viols = _sr.violations(rows, c)
        hard = [v for v in viols if v["hard"] and v["kind"] != "over_max_hours"] + \
               [v for v in viols if v["kind"] == "over_max_hours"][:1]
        for v in sorted(hard, key=lambda x: (x.get("date") or "", x.get("employee") or "")):
            # A breach about a day is named on the day, never charged to
            # whoever's row it is pinned to (schedule_rules.breach_text, E-13).
            if v.get("day_level"):
                add(f"rule:{v['kind']}:{v.get('date')}:{v.get('gap_start') or v.get('floor_role') or ''}",
                    _sr.breach_text(v))
                continue
            where = f"{v.get('day') or v.get('date')} {v.get('shift_start') or ''}".strip()
            add(f"rule:{v['kind']}:{(v.get('employee') or '').strip().lower()}:{v.get('date')}:{v.get('shift_start')}",
                f"{v['employee']} — {where}: {v['detail']}")
        ceiling_seen = set()
        for v in viols:
            if v["hard"]:
                continue
            if v["kind"] == "over_hours_ceiling":
                # One overtime line per person and payroll week, never one per
                # shift of it (it names every row of the week, like the hard
                # maximum above — sched audit 10/10/26 LOGIC-1).
                ck = ((v.get("employee") or "").strip().lower(), v.get("bucket"))
                if ck in ceiling_seen:
                    continue
                ceiling_seen.add(ck)
            if v["kind"] in _sr.ROSTER_BLOCKERS:
                # Nobody counted as a floor manager: the highest rule is
                # unmet on every shift, so the week waits for the owner to
                # name who runs the floor (schedule re-audit 10/4/26 RULES-13).
                add(f"roster:{v['kind']}", v["detail"][:1].upper() + v["detail"][1:])
                continue
            where = f"{v.get('day') or v.get('date')} {v.get('shift_start') or ''}".strip()
            text = f"{v['employee']} — {where}: {v['detail']}"
            if unattended and v["kind"] in _sr.HOLD_UNATTENDED:
                add(f"soft:{v['kind']}:{(v.get('employee') or '').strip().lower()}:{v.get('date')}", text)
            elif text not in soft:
                soft.append(text)
        now = today or _local_today(restaurant_id)
        import schedule_versions as _sv_pb
        import predictability_pay as _pp_pb
        live = None if row["published_at"] else _live_week(restaurant_id, row["week_start"], row["id"])
        # What staff have now: the live copy this draft replaces, or this
        # week's own published copy (ARCH-1) — the base every late change,
        # its predictability pay and the access-to-hours note are read from.
        told = (rows_from_csv(live["schedule_csv"] or "") if live is not None else
                (_sv_pb.published_rows(row["id"], restaurant_id) if row["published_at"] else None))
        if live is not None:
            # A regenerated draft of a week staff already have: what moves
            # is the shifts that differ, and inside the notice window that
            # is the predictability-pay warning — not "the week starts in
            # -2 days" from the notice rule (E-22). Where a law covers the
            # restaurant it is priced per changed shift (MARKET-2).
            changed = _sv_pb.shift_changes(told, rows)["dates"]
            warn = _sr.late_change_warning(c.compliance, changed, now)
            pp = _pp_pb.preview(restaurant_id, told, rows)
            if pp.get("covered"):
                pp_out = pp
                if pp.get("text"):
                    add("late_change:" + ",".join(changed), "This replaces the week staff already have. " + pp["text"])
            elif warn:
                add("late_change:" + ",".join(changed), "This replaces the week staff already have. " + warn)
        elif not row["published_at"]:
            short = _sr.notice_shortfall(c.compliance, row["week_start"], now)
            if short:
                add("notice_short", _notice_text(short, row["week_start"]))
        else:
            # Saved changes to a week staff have, about to be sent.
            pending = _sv_pb.unsent_changes(restaurant_id, row["id"]) or {}
            warn = _sr.late_change_warning(c.compliance, pending.get("dates") or [], now)
            pp = _pp_pb.preview(restaurant_id, told, rows) if told is not None else {"covered": False}
            if pp.get("covered"):
                pp_out = pp
                if pp.get("text"):
                    add("late_change:" + ",".join(pending.get("dates") or []), pp["text"])
            elif warn:
                add("late_change:" + ",".join(pending.get("dates") or []), warn)
        # Hours added straight to someone's week while a law asks for them to
        # be offered to the current staff first (access to hours, MARKET-4):
        # a note, never a hold.
        try:
            import fair_workweek as _fww_pb
            if told is not None:
                for a in _fww_pb.added_hours_without_offer(restaurant_id, _pp_pb.classify(told, rows)):
                    note(f"access_hours:{a['employee'].lower()}:{a['date']}", a["text"])
        except Exception as _ax:
            import ops as _ops_ax
            _ops_ax.capture(_ax, job="publish_access_hours", context=f"restaurant_id={restaurant_id}")
        # A roster with minors and no school calendar for this school year:
        # the default holds them to school-day limits on every weekday but
        # June 1 to July 31 — said once a year (LEARNING-18).
        try:
            import school_calendar as _sc_pb
            ask = _sc_pb.needs_prompt(c.school_calendar, now, bool(c.minors))
            if ask.get("ask"):
                note(f"school_calendar:{ask['school_year']}", ask["text"])
        except Exception:
            pass
    except Exception as e:
        import ops as _ops_pb
        _ops_pb.capture(e, job="publish_blockers_sweep", context=f"restaurant_id={restaurant_id}")
        add("sweep_failed", "The rule check could not run against today's data")
    # The labor budget is a ceiling. Generation trims back to it when the
    # owner's trim_to_budget is on (the default; every removal reported, never
    # below a floor); a week still over it — the trim off, floors that need
    # more, or hours added since — is not trimmed here: it is named, so the
    # owner sends it knowing, or takes hours out first (this comment used to
    # say no week was ever trimmed; schedule audit 10/3/26 P-45). It is an
    # HOURLY budget, so the week's hourly hours are held against it, as they
    # stand now: salaried people's hours are never spent from it, and counting
    # them put a spurious "over the ceiling" of about the salaried hours on
    # every week (E-7, P-6).
    hours = None
    try:
        hours = (_sr.hours_split(rows, c) if c is not None else
                 {"hourly": float(row["hours_hourly"]) if row["hours_hourly"] is not None else
                  float(row["hours_scheduled"] or 0),
                  "salaried": float(row["hours_salaried"] or 0), "total": float(row["hours_scheduled"] or 0)})
        hb = float(row["hours_budget"] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        hb = 0.0
    # Held in dollars when the week can be priced (sched audit 10/10/26
    # LOGIC-13): each person at their own rate, overtime at its premium and
    # an hourly closing manager's assumed stay, against the hourly budget's
    # dollars — the one measure the generation's trim and judge use; hours
    # stay the view beside it.
    dollars = None
    try:
        econ = json.loads(row["economics_json"] or "null") or {}
        lbd = float(econ.get("labor_budget_dollars") or 0)
        if lbd > 0 and c is not None and rows:
            import schedule_economics as _econ
            pricing = _econ.week_pricing(restaurant_id, c, blended_rate=econ.get("blended_rate"))
            cost = float(_econ.price_week(_sr.with_assumed_stays(rows, c), pricing)["total"] or 0)
            dollars = (cost, lbd)
    except Exception as _dx:
        print(f"[publish] dollar budget check unavailable: {_dx!r}")
        dollars = None
    if dollars is not None:
        cost, lbd = dollars
        if cost > lbd * 1.02:
            hs = (hours or {}).get("hourly") or 0.0
            add("hours_over", f"${cost:,.0f} of hourly pay against a ${lbd:,.0f} hourly budget — ${cost - lbd:,.0f} "
                              f"over ({hs:,.0f}h of hourly shifts against {hb:,.0f}h"
                              + (f"; the {hours['salaried']:,.0f}h salaried aren't counted against it)"
                                 if hours and hours.get("salaried") else ")"))
    elif hours and hb > 0 and hours["hourly"] > hb * 1.02:
        hs = hours["hourly"]
        add("hours_over", f"{hs:,.0f}h of hourly shifts against a {hb:,.0f}h hourly budget — {hs - hb:,.0f}h over "
                          "the ceiling" + (f" (the {hours['salaried']:,.0f}h salaried aren't counted against it)"
                                           if hours.get("salaried") else ""))
    try:
        quality = json.loads(row["quality_json"] or "null") or {}
    except Exception:
        quality = {}
    if "checked" in quality and not quality.get("checked"):
        # The quality engine failed on this week: said, with the cause, so
        # a week nobody could score never goes out as if it had passed
        # (P-16).
        cause = str(quality.get("error") or quality.get("reason") or "").strip()
        add("quality_unchecked", "Shift Quality couldn't check this week" + (f" ({cause[:160]})" if cause else "")
            + " — read it yourself before it goes to staff")
    elif quality.get("checked"):
        if quality.get("band") == "weak":
            note("quality_weak", f"Shift Quality {quality.get('score')}/100 — a weak week")
        if (quality.get("confidence") or {}).get("level") == "low":
            note("quality_low", "The quality engine had too little to judge this week on")
        below = quality.get("below_profile") or []
        if below:
            note("below_profile",
                 f"{len(below)} shift{'s' if len(below) != 1 else ''} below the bar set for {'it' if len(below) == 1 else 'them'}")
    # What the owner has sent past before, by key (schedule audit 10/10/26
    # TRUST-28): each blocker's record beside it, the ones they always send
    # past ranked last, a demotable one they always override (the hourly
    # budget, the draft's own review lines — never a rule) moved to a note,
    # and the review of the warnings they always override.
    import schedule_overrides as _so_pr
    return _so_pr.annotate(restaurant_id, {"blockers": out, "soft": soft, "notes": notes, "hours": hours,
                                           "schedule_id": row["id"],
                                           # Each changed shift inside a predictive-scheduling
                                           # law's notice window, priced, and the total
                                           # (MARKET-2) — None when no law covers the
                                           # restaurant or nothing changed.
                                           "predictability_pay": (
                                               {k: pp_out[k] for k in ("law", "code", "total", "owed_count",
                                                                       "unknown_count", "text", "rows")}
                                               if pp_out else None)}, unattended=unattended)


def _drop_share(token, schedule_id):
    """Remove a share link made for a send that did not go out, so the
    status list never shows someone as sent who was not. By the token's
    hash — the only form the row keeps (SEC-09)."""
    _models_mod.drop_schedule_share(token, schedule_id)


# ── telling one person ──────────────────────────────────────────────────────

def _deliver_one(rid, restaurant, schedule_id, csv_text, name, ch, week_label, kind="posted", base_url=None):
    """Tell one person about one week, on the channel they signed up with
    (Friction audit #17): the app when their phone has it, a text when they
    ticked "text me when my schedule is posted", email as the fallback —
    people.deliver, the one rule (employee audit C4/M8). Their own share
    link either way; the portal has the week regardless. `kind`: posted |
    changed | removed (taken off the week: "no shifts"). Returns (status,
    item): "sent" | "unreachable" | "failed", and the line the response and
    the outbox keep.

    Only a delivery that happened counts as sent: deliver() never raises —
    a missing key, a suppressed address, the flood guard and a Resend
    failure all come back as SendResult(ok=False) — and only an exception
    used to count as a failure (F2-1). A share row made for a send that did
    not go out is removed, so the status list never shows that person as
    sent. deliver() records the real outcome in email_log itself."""
    from labor import employee_shifts_from_csv
    import people as _people
    create_schedule_share = _models_mod.create_schedule_share
    ch = ch or {}
    base_url = base_url or config.base_url()
    email = (ch.get("email") or "").strip()
    if not (ch.get("push_user_id") or ch.get("sms") or email):
        return "unreachable", {"employee_name": name, "reason": "no app, text consent or email address on file"}
    shifts = employee_shifts_from_csv(csv_text, name)
    updated = kind != "posted"
    count = f"{len(shifts)} shift{'' if len(shifts) == 1 else 's'}" if shifts else "no shifts"
    posted = "changed" if updated else "is posted"
    box = {}

    def _sms_text(phone):
        box["sms_token"] = create_schedule_share(rid, schedule_id, name, sent_to=phone)
        return (f"{restaurant.name}: your schedule for the week of {week_label} "
                f"{'was updated' if updated else 'is posted'} ({count}). {base_url}/s/{box['sms_token']} "
                f"Reply STOP to stop these texts.")

    def _sms_undo():
        if box.get("sms_token"):
            _drop_share(box.pop("sms_token"), schedule_id)

    def _email_send():
        token = create_schedule_share(rid, schedule_id, name, sent_to=email)
        try:
            from emails import send_staff_schedule_email
            res = send_staff_schedule_email(
                to_email=email, employee_name=name, restaurant_name=restaurant.name,
                week_label=f"{week_label} (updated)" if updated else week_label,
                link=f"{base_url}/s/{token}", shifts=shifts,
                reply_to=restaurant.owner_email or None, restaurant_id=rid)
        except Exception as e:
            res, box["email_error"] = None, str(e)
        if not getattr(res, "ok", False):
            box.setdefault("email_error", getattr(res, "error", None) or "The email was not sent.")
            _drop_share(token, schedule_id)
            return False
        return True

    def _outcome(channel):
        # The app's share row only once the push reached a phone (COM-12).
        if channel == "push":
            try:
                create_schedule_share(rid, schedule_id, name, sent_to="app")
            except Exception as e:
                print(f"[publish] app share not recorded for {name}: {e}")

    first = min((s.get("date") for s in shifts if s.get("date")), default=None)
    body = (f"The week of {week_label} changed: you have no shifts that week now." if kind == "removed"
            else f"The week of {week_label} {posted}: {count}.")
    how = _people.deliver(
        rid, name, ch, alert_type="staff_schedule", title="Your schedule",
        body=body,
        data={"kind": "schedule", "event": "schedule_changed" if updated else "schedule_posted",
              "tab": "today", "schedule_id": schedule_id},
        purpose="schedule", sms_text=_sms_text, sms_undo=_sms_undo,
        sms_meta=lambda: {"share": [schedule_id, box.get("sms_token")]},
        email_send=_email_send, shift_date=first, on_outcome=_outcome)
    if how == "push":
        # Queued to their phone; "delivery: pending" until Apple answers
        # (the app's share row appears then, or a text/email goes instead).
        return "sent", {"employee_name": name, "sent_to": "app", "channel": "push", "delivery": "pending",
                        "shifts": len(shifts)}
    if how in ("sms", "sms_held"):
        return "sent", {"employee_name": name, "sent_to": ch["sms"], "channel": "sms",
                        **({"delivery": "held_until_morning"} if how == "sms_held" else {}), "shifts": len(shifts)}
    if how == "email":
        return "sent", {"employee_name": name, "sent_to": email, "channel": "email", "shifts": len(shifts)}
    if email:
        return "failed", {"employee_name": name, "error": box.get("email_error") or "The email was not sent."}
    return "unreachable", {"employee_name": name, "reason": "no app, text consent or email address on file"}


def _send_week_to_staff(rid, restaurant, schedule_id, csv_text, names, week_label, contacts=None,
                        updated=False):
    """(sent, unreachable, failed) for telling `names` about one week
    directly, without the outbox — kept for a caller that needs the old
    shape; publishing goes through the outbox (enqueue, deliver_notices)."""
    import people as _people
    try:
        reachable = _people.reach(rid, names, purpose="schedule")
    except Exception as _re:
        _ops.capture(_re, job="schedule_publish_reach", context=f"restaurant_id={rid}")
        if contacts is None:
            contacts = {c["employee_name"].lower(): c for c in _models_mod.get_staff_contacts(rid)}
        reachable = {n: {"push_user_id": None, "sms": None,
                         "email": ((contacts.get(n.lower()) or {}).get("email") or "").strip() or None}
                     for n in names}
    out = {"sent": [], "unreachable": [], "failed": []}
    for name in names:
        status, item = _deliver_one(rid, restaurant, schedule_id, csv_text, name, reachable.get(name) or {},
                                    week_label, kind="changed" if updated else "posted")
        out[status].append(item)
    return out["sent"], out["unreachable"], out["failed"]


# ── the outbox ──────────────────────────────────────────────────────────────

def enqueue(conn, restaurant_id, history_id, week_start, batch, items, requested_by=None, person_ids=None) -> list:
    """Queue one notice per (name, kind) of one event, on the caller's
    connection inside its transaction (the stamp's). A person's earlier
    notice about the same week that has not gone yet is cancelled — this
    one replaces it, and it is told from the week as it stands when it
    goes. Returns [(name, kind)] queued."""
    queued = []
    waiting = conn.execute("SELECT id, employee_name FROM schedule_notices WHERE restaurant_id=? AND week_start=? "
                           "AND status IN ('pending', 'failed')", (restaurant_id, week_start)).fetchall()
    for name, kind in items:
        name = " ".join(str(name or "").split())
        if not name:
            continue
        k = _fold(name)
        for w in waiting:
            if _fold(w["employee_name"]) == k:
                conn.execute("UPDATE schedule_notices SET status='cancelled', next_attempt_at=NULL WHERE id=? "
                             "AND status IN ('pending', 'failed')", (w["id"],))
        pid = (person_ids or {}).get(name)
        conn.execute("INSERT OR IGNORE INTO schedule_notices (restaurant_id, history_id, week_start, batch, "
                     "employee_name, person_id, kind, requested_by) VALUES (?,?,?,?,?,?,?,?)",
                     (restaurant_id, history_id, week_start, batch, name, int(pid) if pid else None, kind,
                      (requested_by or "")[:120] or None))
        queued.append((name, kind))
    return queued


def _due_sql(alias="n"):
    a = alias
    return (f"({a}.status='pending' OR ({a}.status='failed' AND {a}.next_attempt_at IS NOT NULL "
            f"AND {a}.next_attempt_at<=?) OR ({a}.status='sending' AND {a}.claimed_at<?))")


def deliver_notices(restaurant_id=None, batch=None, history_id=None, budget_seconds=INLINE_SECONDS,
                    limit=None, db_path=None) -> dict:
    """Deliver the notices that are due — of one batch (the Send that just
    stamped), one week, one restaurant, or all — oldest first, for at most
    `budget_seconds`. Each is claimed (a delivery that died is taken over
    after NOTICE_LEASE_MINUTES), told from the week's PUBLISHED copy as it
    stands now, and its result kept: sent, unreachable (nothing on file) or
    failed (retried at NOTICE_RETRY_MINUTES, then left failed for the
    owner's Send again). Resumable by construction — a notice leaves the
    due set once it is decided, so the next pass starts with what this one
    did not reach. Returns {"sent", "unreachable", "failed": [items],
    "pending": [names still to go], "attempted", "hit_bound"}."""
    import people as _people
    started = _time.monotonic()
    out = {"sent": [], "unreachable": [], "failed": [], "pending": [], "attempted": 0, "hit_bound": False}
    filters, fargs = [], []
    if restaurant_id is not None:
        filters.append("n.restaurant_id=?")
        fargs.append(int(restaurant_id))
    if batch is not None:
        filters.append("n.batch=?")
        fargs.append(batch)
    if history_id is not None:
        filters.append("n.history_id=?")
        fargs.append(int(history_id))
    where = " AND ".join([_due_sql()] + filters)
    base_url = config.base_url()
    ctx = {}
    last_id = 0            # this pass's cursor: each notice is looked at once
    while True:
        if _time.monotonic() - started > budget_seconds:
            out["hit_bound"] = True
            break
        if limit is not None and out["attempted"] >= limit:
            out["hit_bound"] = True
            break
        now = _dt.utcnow()
        conn = get_conn(db_path)
        try:
            rows = conn.execute(f"SELECT n.* FROM schedule_notices n WHERE {where} AND n.id>? ORDER BY n.id LIMIT 25",
                                [_utc(now), _utc(now - _td(minutes=NOTICE_LEASE_MINUTES))] + fargs + [last_id]
                                ).fetchall()
        finally:
            conn.close()
        if not rows:
            break
        for n in rows:
            if _time.monotonic() - started > budget_seconds or (limit is not None and out["attempted"] >= limit):
                out["hit_bound"] = True
                break
            last_id = n["id"]
            now = _dt.utcnow()
            conn = get_conn(db_path)
            try:
                got = conn.execute(
                    f"UPDATE schedule_notices SET status='sending', claimed_at=?, attempts=attempts+1 "
                    f"WHERE id=? AND {_due_sql('schedule_notices')}",
                    (_utc(now), n["id"], _utc(now), _utc(now - _td(minutes=NOTICE_LEASE_MINUTES)))).rowcount
                conn.commit()
            finally:
                conn.close()
            if not got:
                continue
            out["attempted"] += 1
            key = (n["restaurant_id"], n["history_id"])
            if key not in ctx:
                ctx[key] = _notice_context(n["restaurant_id"], n["history_id"], db_path)
            c = ctx[key]
            if c is None:
                _finish_notice(n["id"], "cancelled", error="the week is gone or was never sent", db_path=db_path)
                continue
            if n["employee_name"] not in c["reach"]:
                try:
                    c["reach"].update(_people.reach(n["restaurant_id"], [n["employee_name"]], purpose="schedule"))
                except Exception as e:
                    _ops.capture(e, job="schedule_publish_reach", context=f"restaurant_id={n['restaurant_id']}")
                    c["reach"][n["employee_name"]] = {"push_user_id": None, "sms": None, "email": None}
            try:
                status, item = _deliver_one(n["restaurant_id"], c["restaurant"], n["history_id"], c["csv"],
                                            n["employee_name"], c["reach"].get(n["employee_name"]), c["week_label"],
                                            kind=n["kind"], base_url=base_url)
            except Exception as e:
                _ops.capture(e, job="schedule_notice", context=f"restaurant_id={n['restaurant_id']} notice={n['id']}")
                status, item = "failed", {"employee_name": n["employee_name"], "error": str(e)[:200]}
            _finish_notice(n["id"], status, item=item, attempts=int(n["attempts"] or 0) + 1, db_path=db_path)
            out[status].append(item)
    # What this pass did not reach, of the same filter.
    conn = get_conn(db_path)
    try:
        q = "SELECT employee_name FROM schedule_notices n WHERE n.status IN ('pending', 'sending')"
        if filters:
            q += " AND " + " AND ".join(filters)
        out["pending"] = [r["employee_name"] for r in conn.execute(q + " ORDER BY n.id", fargs).fetchall()]
    finally:
        conn.close()
    return out


def _notice_context(restaurant_id, history_id, db_path=None):
    """What telling anyone about one week needs: the restaurant, the week's
    label and its PUBLISHED copy as it stands now. None when the week is
    gone or was never published."""
    from time_utils import mdy_range as _mdy_range
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT week_start, week_end, published_at, COALESCE(published_csv, schedule_csv) AS live "
                           "FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (history_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    restaurant = _models_mod.get_restaurant(restaurant_id)
    if not row or not row["published_at"] or not restaurant:
        return None
    return {"restaurant": restaurant, "csv": row["live"] or "", "reach": {},
            "week_label": _mdy_range(row["week_start"], row["week_end"] or row["week_start"])}


def _finish_notice(notice_id, status, item=None, attempts=1, error=None, db_path=None):
    item = item or {}
    conn = get_conn(db_path)
    try:
        if status == "sent":
            conn.execute("UPDATE schedule_notices SET status='sent', channel=?, sent_to=?, delivery=?, shifts=?, "
                         "sent_at=datetime('now'), last_error=NULL, next_attempt_at=NULL, claimed_at=NULL WHERE id=?",
                         (item.get("channel"), item.get("sent_to"), item.get("delivery"), item.get("shifts"),
                          notice_id))
        elif status == "failed":
            retry = None
            if attempts < NOTICE_MAX_ATTEMPTS:
                retry = _utc(_dt.utcnow() + _td(minutes=NOTICE_RETRY_MINUTES[min(attempts, len(NOTICE_RETRY_MINUTES)) - 1]))
            conn.execute("UPDATE schedule_notices SET status='failed', last_error=?, next_attempt_at=?, claimed_at=NULL "
                         "WHERE id=?", ((item.get("error") or error or "not sent")[:300], retry, notice_id))
        else:
            conn.execute("UPDATE schedule_notices SET status=?, last_error=?, next_attempt_at=NULL, claimed_at=NULL "
                         "WHERE id=?", (status, (item.get("reason") or error or "")[:300] or None, notice_id))
        conn.commit()
    finally:
        conn.close()


def run_schedule_notices(max_seconds: float = NOTICE_JOB_SECONDS, db_path=None) -> dict:
    """The `schedule_notices` job: deliver every notice that is due —
    what a Send's inline window did not reach, and the failures whose retry
    time came — bounded by `max_seconds`, resumable (a decided notice
    leaves the due set). {attempted, ok, failed, skipped, hit_bound}."""
    got = deliver_notices(budget_seconds=max_seconds, db_path=db_path)
    return {"attempted": got["attempted"], "ok": len(got["sent"]), "failed": len(got["failed"]),
            "skipped": len(got["unreachable"]), "hit_bound": bool(got["hit_bound"])}


def notice_status(restaurant_id, history_id, db_path=None) -> dict:
    """Who has been told about this week and how it went, from the outbox:
    {"people": [{employee_name, status, kind, channel, sent_to, error,
    attempts, next_attempt_at, sent_at}] (each person's newest notice),
    "sending": n, "failed": n, "unreachable": n}."""
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute("SELECT * FROM schedule_notices WHERE restaurant_id=? AND history_id=? "
                                "AND status <> 'cancelled' ORDER BY id", (restaurant_id, history_id)).fetchall()
        except Exception:
            rows = []
    finally:
        conn.close()
    newest = {}
    for r in rows:
        newest[_fold(r["employee_name"])] = r
    people_ = [{"employee_name": r["employee_name"], "status": r["status"], "kind": r["kind"],
                "channel": r["channel"], "sent_to": r["sent_to"], "error": r["last_error"],
                "attempts": r["attempts"], "next_attempt_at": r["next_attempt_at"], "sent_at": r["sent_at"]}
               for r in newest.values()]
    return {"people": people_,
            "sending": sum(1 for p in people_ if p["status"] in ("pending", "sending")),
            "failed": sum(1 for p in people_ if p["status"] == "failed"),
            "unreachable": sum(1 for p in people_ if p["status"] == "unreachable")}


# ── the stamp ───────────────────────────────────────────────────────────────

class _Refused(Exception):
    """The stamp found the week moved under the Send: nothing was written."""

    def __init__(self, payload, status):
        super().__init__(payload.get("error") or "refused")
        self.payload, self.status = payload, status


def _recipients(old_live, new_rows) -> list:
    """[(name, kind)] to tell when `new_rows` replace what staff have
    (`old_live`, None on a first send): everyone on a first send; else only
    the people whose shifts differ — "removed" for those taken off the week
    (BUGSLIFE-7), "posted" for those new to it, "changed" for the rest."""
    import schedule_rows as _srows
    from labor import employees_in_schedule
    if old_live is None:
        return [(n, "posted") for n in employees_in_schedule(_srows.to_csv(new_rows))]
    pc = _srows.people_changed(old_live, new_rows)
    return [(n, "removed" if n in pc["removed"] else "posted" if n in pc["added"] else "changed")
            for n in pc["changed"]]


def stale_payload(restaurant_id, schedule_id, version):
    """A Send of rows the sender no longer has on screen (BUGSLIFE-11): who
    changed the week since `version` and how — the save conflict's shape.
    None when the rows are still the rows of `version`."""
    import schedule_versions as _sv
    conn = get_conn()
    try:
        moved = _sv.rows_changed_since(conn, schedule_id, version)
    finally:
        conn.close()
    if not moved:
        return None
    try:
        base = int(version)
    except (TypeError, ValueError):
        base = 0
    later = [v for v in _sv.list_versions(restaurant_id, schedule_id) if v["version"] > base and v.get("changes")]
    who = later[-1] if later else {}
    lines = [ln for v in later for ln in (v.get("lines") or [])][:6]
    return {"ok": False, "conflict": True, "stale": True, "schedule_id": schedule_id, "latest_version": moved,
            "saved_by": who.get("saved_by"), "lines": lines,
            "error": f"{who.get('saved_by') or 'Somebody'} changed this week after you opened it, so it wasn't "
                     "sent. Look at their changes, then send."}


def _stamp(restaurant_id, schedule_id, mode, actor_name, authority, gate_sig, version=None, db_path=None):
    """Publish (mode "publish") or send the changes to (mode "changes")
    one week, in ONE write transaction: re-read under the lock, refuse
    when the copy was replaced or its rows moved since the gate read them
    (or since `version`, the rows the sender reviewed), set the published
    copy from exactly those rows, retire the week's older published copy,
    append the `published` version and queue the notices. Returns {version,
    batch, queued, week_start, rows}; raises _Refused."""
    import schedule_rows as _srows
    import schedule_versions as _sv
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id, week_start, week_end, schedule_csv, published_csv, published_at, superseded_by "
                           "FROM schedule_history WHERE id=? AND restaurant_id=?", (schedule_id, restaurant_id)).fetchone()
        if row is None:
            raise _Refused({"ok": False, "schedule_id": schedule_id,
                            "error": "That week is gone — reload the schedule."}, 404)
        if mode == "publish" and row["published_at"]:
            raise _Refused({"ok": True, "already_published": True, "schedule_id": schedule_id}, 200)
        if _sv.replaced_error(row):
            raise _Refused({"ok": False, "replaced": True, "superseded_by": row["superseded_by"],
                            "schedule_id": schedule_id, "error": _sv.replaced_error(row)}, 409)
        if mode == "changes" and not row["published_at"]:
            raise _Refused({"ok": False, "schedule_id": schedule_id,
                            "error": "This week hasn't been sent to staff yet — send the whole week."}, 400)
        if version is not None and _sv.rows_changed_since(conn, schedule_id, version):
            raise _Refused({"ok": False, "stale": True, "schedule_id": schedule_id}, 409)
        if gate_sig is not None and _sv._rows_sig(row["schedule_csv"]) != gate_sig:
            # A save landed between the gate and the stamp: what would go
            # out is not what was checked (ARCH-21, BUGSLIFE-6).
            raise _Refused({"ok": False, "changed": True, "schedule_id": schedule_id,
                            "error": "The week changed while it was being checked, so it wasn't sent. "
                                     "Look at it again, then send."}, 409)
        rows = _srows.parse(row["schedule_csv"])
        if mode == "publish":
            live = _models_mod.live_week_row(conn, restaurant_id, row["week_start"])
            old_live = _srows.parse(live["schedule_csv"]) if (live and live["id"] != schedule_id
                                                              and live["published_at"]) else None
            if old_live is not None:
                # The replacement keeps the ids of the shifts it keeps.
                rows = _srows.carry_ids(rows, old_live, known_ids=[r.get("shift_id") for r in rows])
            text = _srows.to_csv(rows)
            conn.execute("UPDATE schedule_history SET published_at=datetime('now'), published_by=?, published_csv=?, "
                         "schedule_csv=?, publishing_at=NULL WHERE id=? AND restaurant_id=?",
                         (actor_name, text, text, schedule_id, restaurant_id))
            # One live version of a week: an older published copy is retired,
            # or both fed outcomes, fairness, hours and claims (SCHED-10).
            conn.execute("UPDATE schedule_history SET superseded_by=? WHERE restaurant_id=? AND week_start=? "
                         "AND id<>? AND published_at IS NOT NULL AND superseded_by IS NULL",
                         (schedule_id, restaurant_id, row["week_start"], schedule_id))
        else:
            old_live, _rebuilt = _sv._live_rows_of(conn, schedule_id, restaurant_id, row)
            old_live = _srows.carry_ids(old_live, rows, known_ids=[r.get("shift_id") for r in old_live])
            text = _srows.to_csv(rows)
            conn.execute("UPDATE schedule_history SET published_csv=?, republished_at=datetime('now') "
                         "WHERE id=? AND restaurant_id=?", (text, schedule_id, restaurant_id))
            # A shift the Studio moved to someone else keeps its floor
            # section now that staff have the change.
            try:
                _models_mod.carry_shift_sections(conn, restaurant_id, old_live, rows)
            except Exception as e:
                if "no such table" not in str(e).lower():
                    raise
        # The live requests on the week's dates follow the copy staff now
        # have, in this same transaction — a retimed shift carries its
        # request, a removed or reassigned one closes it (ARCH-9); the
        # requesters are told after the commit (_after_send).
        try:
            import shift_requests as _srq_f
            _srq_f.follow_week(conn, restaurant_id, rows,
                               dates=_sv._week_dates(row["week_start"], row["week_end"] or row["week_start"]))
        except Exception as e:
            log.warning("requests not followed for week %s: %s", schedule_id, e)
        items = _recipients(old_live, rows)
        if mode == "changes" and not items:
            raise _Refused({"ok": True, "already_published": True, "schedule_id": schedule_id}, 200)
        _row_id, new_version = _sv._insert_version(conn, restaurant_id, schedule_id, "published", text,
                                                   saved_by=actor_name, saved_authority=authority)
        batch = _uuid.uuid4().hex
        person_ids = {" ".join(str(r.get("employee") or "").split()): r.get("person_id") for r in rows
                      if r.get("person_id")}
        queued = enqueue(conn, restaurant_id, schedule_id, row["week_start"], batch, items,
                         requested_by=actor_name, person_ids=person_ids)
        conn.commit()
        return {"version": new_version, "batch": batch, "queued": queued, "week_start": row["week_start"],
                "rows": rows, "replaced_live": old_live is not None and mode == "publish"}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _rows_sig_now(restaurant_id, schedule_id):
    """The week's working-copy rows as they stand now, as one comparable value."""
    import schedule_versions as _sv
    conn = get_conn()
    try:
        row = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (schedule_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    return _sv._rows_sig(row["schedule_csv"] if row else "")


def _claim(restaurant_id, schedule_id, mode):
    """Take the week's Send claim (SCHED-29, DATA-12): one Send at a time.
    A claim older than PUBLISH_CLAIM_MINUTES belongs to a Send that died.
    Returns whether this Send holds it."""
    cond = "AND published_at IS NULL " if mode == "publish" else ""
    conn = get_conn()
    try:
        _models_mod._ensure_history_columns(conn)
        got = conn.execute(
            "UPDATE schedule_history SET publishing_at=datetime('now') WHERE id=? AND restaurant_id=? "
            + cond + "AND superseded_by IS NULL AND (publishing_at IS NULL OR publishing_at < "
            f"datetime('now','-{_models_mod.PUBLISH_CLAIM_MINUTES} minutes'))",
            (schedule_id, restaurant_id)).rowcount
        conn.commit()
        return bool(got)
    finally:
        conn.close()


def send_in_progress(restaurant_id, week_start=None, history_id=None) -> bool:
    """Whether a Send holds this week right now (its copy's publishing_at
    claim is fresh) — `history_id`'s copy, else the week's working copy.
    Generate waits for it (BUGSLIFE-2). Never raises (False)."""
    try:
        conn = get_conn()
        try:
            if history_id:
                row = conn.execute("SELECT id FROM schedule_history WHERE id=? AND restaurant_id=? AND "
                                   + _models_mod.publish_claim_fresh_sql(), (int(history_id), restaurant_id)).fetchone()
            elif week_start:
                draft = _models_mod.week_draft(conn, restaurant_id, str(week_start)[:10])
                row = None if draft is None else conn.execute(
                    "SELECT id FROM schedule_history WHERE id=? AND " + _models_mod.publish_claim_fresh_sql(),
                    (draft["id"],)).fetchone()
            else:
                row = None
        finally:
            conn.close()
        return row is not None
    except Exception:
        return False


def _release(restaurant_id, schedule_id):
    """Let the week's claim go. A Send that ended without publishing leaves
    the copy as it was — and when a generation landed while the claim held
    it off (BUGSLIFE-2), the newer draft is the week's draft now: this copy
    is superseded by it, as the generation would have done."""
    conn = get_conn()
    try:
        conn.execute("UPDATE schedule_history SET publishing_at=NULL WHERE id=? AND restaurant_id=?",
                     (schedule_id, restaurant_id))
        row = conn.execute("SELECT week_start, published_at, superseded_by FROM schedule_history WHERE id=? "
                           "AND restaurant_id=?", (schedule_id, restaurant_id)).fetchone()
        if row and not row["published_at"] and not row["superseded_by"] and row["week_start"]:
            newer = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? AND week_start=? AND id>? "
                                 "AND published_at IS NULL AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
                                 (restaurant_id, row["week_start"], schedule_id)).fetchone()
            if newer:
                conn.execute("UPDATE schedule_history SET superseded_by=? WHERE id=? AND published_at IS NULL "
                             "AND superseded_by IS NULL", (newer["id"], schedule_id))
        conn.commit()
    finally:
        conn.close()


def _after_publish(rid, schedule_id, row, actor, actor_name, authority):
    """What a first publish records once it is stamped — none of it can
    hold the publish."""
    # The owner is acting. Measure what it does to labor % over the next
    # window, whether or not they ever pressed Track (outcomes.observe).
    try:
        import outcomes as _oc
        _oc.observe(rid, "schedule_published", detail=f"week of {row['week_start']}",
                    user_id=actor.get("id"))
    except Exception as _oe:
        _ops.capture(_oe, job="observe_schedule", context=f"restaurant_id={rid}")
    # The queue's "this week is built but not sent" is now carried out
    # (rec_ledger implemented, ROI #27) — only if it was ever shown.
    try:
        import rec_ledger as _rl_pub
        _rl_pub.implemented(rid, _rl_pub.rec_key("schedule_unsent", schedule_id), "schedule_review",
                            user_id=actor.get("id"), source_ref=f"published:{schedule_id}",
                            meta={"module": "schedule"})
    except Exception as _rle:
        print(f"[publish] implementation not recorded for {rid}: {_rle}")
    # What the week's first publish says, into the observation log: every
    # change from the original draft by origin (the manager's, Cavnar AI's
    # kept ones, an admin's), and whether a person sent it (schedule audit
    # 10/3/26 L-4, L-5, L-7). Never blocks the publish.
    try:
        import schedule_versions as _sv_obs
        _sv_obs.observe_publish(rid, schedule_id, authority=authority, editor=actor_name)
    except Exception as _obx:
        _ops.capture(_obx, job="schedule_publish_observe", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    # What the restaurant did with the generated week, on the labor_schedule
    # run that wrote it: the share of its shifts that went out untouched
    # (re-audit 10/7/26 #4). Never blocks the publish.
    try:
        import schedule_versions as _sv_out
        _sv_out.record_week_outcome(rid, schedule_id, actor=actor, published=True)
    except Exception as _wox:
        print(f"[publish] schedule outcome not filed for {rid}: {_wox}")
    # The reviews' and the nightly reports' "+1" asks this week carries,
    # recorded as implemented under it — the loop from a complaint to the
    # person added and what the complaints did next (re-audit CROSSMODULE-10).
    try:
        import staffing_signals as _stsig_pub
        _stsig_pub.record_published(rid, schedule_id, row["schedule_csv"],
                                    user_id=actor.get("id") if isinstance(actor, dict) else None)
    except Exception as _spx:
        print(f"[publish] staffing asks not recorded for {rid}: {_spx}")
    # The week's sales projection the schedule was built against, frozen
    # now so it can be scored when the week closes (forecast_log kind
    # revenue_week, insert-once; CA2 #6). Never blocks the publish.
    try:
        import demand as _demand
        _demand.freeze_week_projection(rid, row["week_start"])
    except Exception as _fx:
        print(f"[schedule] week projection not frozen rid={rid}: {_fx}")


def _after_send(rid, schedule_id, version, review, acked, override_reasons, actor, authority):
    """What every stamped Send records — none of it can hold the send:
    the warnings it went out past, by key, with the published version and
    any reason (schedule audit 10/10/26 TRUST-28 — what lets the list learn
    which ones this owner always overrides); and the live requests on the
    week's dates follow the copy staff now have — a retimed shift carries
    its request, a removed one closes it and its requester is told (ARCH-9)."""
    try:
        import schedule_overrides as _so_pub
        _so_pub.record_overridden(rid, schedule_id, version, review.get("blockers") or [], acked or [],
                                  reasons=override_reasons, actor=actor if isinstance(actor, dict) else None,
                                  authority=authority)
    except Exception as _ox:
        _ops.capture(_ox, job="schedule_overrides", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    try:
        import shift_requests as _srq_follow
        _srq_follow.reconcile(rid)
    except Exception as _fx:
        _ops.capture(_fx, job="shift_requests_follow", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    # How firm each published shift is, recorded with the send (commitment_
    # ladder, scheduler audit 10/10/26 MARKET-27): staff see firm / likely /
    # tentative, and a changes send marks the committed rows it changed. The
    # nightly schedule_bench pass records any week this missed.
    try:
        import commitment_ladder as _cl_pub
        _cl_pub.ensure(rid, schedule_id)
    except Exception as _clx:
        _ops.capture(_clx, job="commitment_ladder", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    # The week to the POS time clock where the owner turned that on — queued
    # for the pos_schedule_push job, never waited on, never raising
    # (schedule audit 10/10/26 MARKET-7).
    import pos_schedule_push as _psp
    _psp.after_publish(rid, schedule_id, actor=actor)


def _record_pay(rid, schedule_id, rows, sent_by, before_rows=None, replacement=False):
    """On the predictability-pay record: each changed shift inside a
    predictive-scheduling law's notice window, priced, against what staff
    had (`before_rows`: the live copy a replacement retires, or this week's
    published copy before a changes send), and any consented short rest's
    premium (MARKET-2, MARKET-3). A first send with nothing replaced records
    only the rest premiums. Never stops the send."""
    try:
        import predictability_pay as _pp_pub
        if before_rows is not None:
            _pp_pub.record_send(rid, schedule_id, rows, before_rows=before_rows, sent_by=sent_by)
        _pp_pub.record_rest_premiums(rid, schedule_id, None, rows, sent_by=sent_by)
    except Exception as _ppx:
        _ops.capture(_ppx, job="predictability_pay_record", context=f"restaurant_id={rid} schedule_id={schedule_id}")


def _actor_name(actor) -> str:
    return (actor.get("username") or actor.get("email") or "automation") if isinstance(actor, dict) else "automation"


def _delivered_payload(rid, schedule_id, got) -> dict:
    """The response's part about who was told: sent / unreachable / failed
    as the Send found them, `sending` for the people still to go (the
    outbox job reaches them within the minute) and the share status."""
    return {"sent": got["sent"], "unreachable": got["unreachable"], "failed": got["failed"],
            "sending": got["pending"], "status": _models_mod.get_schedule_share_status(rid, schedule_id),
            "delivery": notice_status(rid, schedule_id)}


def publish(restaurant_id, schedule_id=None, actor=None, acknowledge=False, version=None, override_reasons=None):
    """Send a week to staff for the first time — the route, the mobile twin,
    the delayed run and auto-publish. Returns (payload, http_status).
    `actor` is the user dict acting, or delayed.AUTOMATION_ACTOR;
    `version` the version of the rows the sender reviewed (a Send of rows
    somebody changed since is refused with their changes, BUGSLIFE-11).
    `override_reasons` ({key: reason}) are kept with the keys the send went
    out past (schedule_overrides, TRUST-28).

    A schedule with blockers (publish_blockers) is refused with
    needs_ack=True until the caller says acknowledge — a human reading the
    list, never automation. Publishing stamps published_at and the
    published copy staff read, in one transaction with the notices it
    queues; the notices are delivered after it (see the module's doc)."""
    import schedule_versions as _sv
    from time_utils import mdy_range as _mdy_range
    actor = actor or {}
    rid = restaurant_id
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    conn = get_conn()
    try:
        cols = "id, week_start, week_end, schedule_csv, superseded_by, published_at"
        if schedule_id:
            row = conn.execute(f"SELECT {cols} FROM schedule_history WHERE id=? AND restaurant_id=?",
                               (int(schedule_id), rid)).fetchone()
        else:
            row = _models_mod.newest_draft(conn, rid, columns=cols)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Which schedule?"}, 400
    finally:
        conn.close()

    if not row or not (row["schedule_csv"] or "").strip():
        return {"ok": False, "error": "Generate a schedule first — there's nothing to send yet."}, 400
    # A copy of the week a newer one replaced is never sent — by a person,
    # a stale phone, or the delayed run of a Send queued before the newer
    # draft (schedule re-audit 10/4/26 UI-3).
    _replaced = _sv.replaced_error(row)
    if _replaced:
        return {"ok": False, "replaced": True, "superseded_by": row["superseded_by"],
                "schedule_id": row["id"], "error": _replaced}, 409
    schedule_id = row["id"]
    if version is not None and not row["published_at"]:
        stale = stale_payload(rid, schedule_id, version)
        if stale:
            return stale, 409

    # `acknowledge` is True (a person read the list just now) or the list
    # of blocker keys a person acknowledged when they queued it (a delayed
    # publish): a blocker that appeared since is not covered (NS5 H3).
    unattended = acknowledge is False and bool(actor.get("role") == "automation")
    gate_sig = _sv._rows_sig(row["schedule_csv"])
    review = publish_review(rid, schedule_id, unattended=unattended)
    blockers = [b["text"] for b in review["blockers"]]
    _acked, unacked = _ack_gate(review, acknowledge)
    if unacked:
        # Each blocker with its key, so the client acknowledges exactly the
        # list it showed (F2-9).
        return _needs_ack(review, unacked, schedule_id)

    # What staff read in the push, the text and the email subject: M/D/YY,
    # never the ISO the row stores (F2-15).
    week_label = _mdy_range(row["week_start"], row["week_end"] or row["week_start"])

    # Claim the week before anything is written. Nothing stopped a second
    # publish (a double tap, a second device, the 11am auto-publish after a
    # 10am manual one) from emailing every member of staff again (SCHED-29,
    # DATA-12).
    if not _claim(rid, schedule_id, "publish"):
        conn = get_conn()
        try:
            state = conn.execute("SELECT published_at, superseded_by FROM schedule_history WHERE id=?",
                                 (schedule_id,)).fetchone()
            _v_now = _sv.latest_version(conn, schedule_id)
        finally:
            conn.close()
        if state and state["published_at"]:
            return dict(ok=True, already_published=True, schedule_id=schedule_id, week_label=week_label,
                        sent=[], unreachable=[], failed=[], sending=[], version=_v_now or None,
                        status=_models_mod.get_schedule_share_status(rid, schedule_id),
                        error=None), 200
        if state and state["superseded_by"]:
            return {"ok": False, "replaced": True, "superseded_by": state["superseded_by"],
                    "schedule_id": schedule_id, "error": _sv.replaced_error(state)}, 409
        return {"ok": False, "in_progress": True, "schedule_id": schedule_id,
                "error": "This week is being sent to staff right now."}, 409

    actor_name = _actor_name(actor)
    authority = _sv.authority_of(actor)
    # The copy this one replaces, read before the stamp retires it: its
    # changes go on the predictability-pay record (MARKET-2).
    _replacing = _live_week(rid, row["week_start"], schedule_id)
    regates = 0
    while True:
        try:
            stamped = _stamp(rid, schedule_id, "publish", actor_name, authority, gate_sig, version=version)
            break
        except _Refused as r:
            if r.payload.get("changed") and version is None and regates < GATE_RETRIES:
                # A save landed while the gate ran (BUGSLIFE-6, ARCH-21): what
                # goes out is the week as saved — checked again, the claim
                # still held, never the rows read before the save.
                regates += 1
                gate_sig = _rows_sig_now(rid, schedule_id)
                review = publish_review(rid, schedule_id, unattended=unattended)
                blockers = [b["text"] for b in review["blockers"]]
                _acked, unacked = _ack_gate(review, acknowledge)
                if unacked:
                    _release(rid, schedule_id)
                    return _needs_ack(review, unacked, schedule_id)
                continue
            _release(rid, schedule_id)
            if r.payload.get("already_published"):
                return dict(r.payload, week_label=week_label, sent=[], unreachable=[], failed=[], sending=[],
                            status=_models_mod.get_schedule_share_status(rid, schedule_id), error=None), 200
            if r.payload.get("stale"):
                return (stale_payload(rid, schedule_id, version) or r.payload), 409
            return r.payload, r.status
        except Exception:
            _release(rid, schedule_id)
            raise

    log_account_event(rid, "schedule_published", actor,
                      detail=f"{len(stamped['queued'])} to tell" + (" — acknowledged blockers" if blockers else ""))
    _after_send(rid, schedule_id, stamped["version"], review, _acked, override_reasons, actor, authority)
    _record_pay(rid, schedule_id, stamped["rows"], actor_name,
                before_rows=_sv.rows_from_csv(_replacing["schedule_csv"] or "") if _replacing is not None else None,
                replacement=True)
    import schedule_rows as _srows
    _after_publish(rid, schedule_id, dict(row, schedule_csv=_srows.to_csv(stamped["rows"])), actor, actor_name,
                   authority)
    got = deliver_notices(restaurant_id=rid, batch=stamped["batch"], budget_seconds=INLINE_SECONDS)
    note = None
    if not got["sent"] and not got["pending"]:
        if got["failed"]:
            note = ("Published to the staff portal. The messages couldn't be sent just now — Cavnar AI tries "
                    "again in a few minutes, and Send again reaches them too.")
        elif stamped["replaced_live"] and not stamped["queued"]:
            note = "Published. Nobody's shifts changed from the copy staff already had, so nobody was told."
        else:
            note = ("Published to the staff portal. Nobody has the app, agreed to texts, or has an email "
                    "address on file, so nothing went out.")
    return dict(ok=True, schedule_id=schedule_id, week_label=week_label,
                acknowledged=bool(blockers), portal_only=not got["sent"] and not got["pending"], note=note,
                version=stamped["version"], error=None, **_delivered_payload(rid, schedule_id, got)), 200


def send_changes(restaurant_id, schedule_id, actor=None, acknowledge=False, version=None, override_reasons=None):
    """Tell the people whose shifts changed on a week staff already have —
    the explicit second step after a save (F2-2). Returns (payload, status).

    A save only records the edit on the working copy; this runs the publish
    gate, then — in one transaction — sets the published copy to the saved
    week (the version the sender reviewed, BUGSLIFE-11), appends a
    `published` version and queues a notice for each person whose shifts
    differ from what they were told, the people taken off the week
    included. The caller checks SCHEDULE_PUBLISH and the undo window
    (client_api._publish_schedule_request)."""
    import schedule_versions as _sv
    from time_utils import mdy_range as _mdy_range
    actor = actor or {}
    rid = restaurant_id
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, week_start, week_end, schedule_csv, published_at, superseded_by "
                           "FROM schedule_history WHERE id=? AND restaurant_id=?", (int(schedule_id), rid)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That week is gone — reload the schedule."}, 404
    # Changes to a copy a newer one replaced reach nobody's live week (UI-3).
    if _sv.replaced_error(row):
        return {"ok": False, "replaced": True, "superseded_by": row["superseded_by"], "schedule_id": row["id"],
                "error": _sv.replaced_error(row)}, 409
    if not row["published_at"]:
        return {"ok": False, "error": "This week hasn't been sent to staff yet — send the whole week."}, 400
    week_label = _mdy_range(row["week_start"], row["week_end"] or row["week_start"])
    pending = _sv.unsent_changes(rid, row["id"]) or {"people": [], "dates": []}
    if not pending["people"]:
        _vn = _sv.newest_version(rid, row["id"])
        return dict(ok=True, already_published=True, schedule_id=row["id"], week_label=week_label,
                    sent=[], unreachable=[], failed=[], sending=[], unsent_changes=[],
                    version=_vn[0]["version"] if _vn else None,
                    status=_models_mod.get_schedule_share_status(rid, row["id"]), error=None), 200
    if version is not None:
        stale = stale_payload(rid, row["id"], version)
        if stale:
            return stale, 409
    gate_sig = _sv._rows_sig(row["schedule_csv"])
    review = publish_review(rid, row["id"])
    _acked, unacked = _ack_gate(review, acknowledge)
    if unacked:
        return _needs_ack(review, unacked, row["id"])
    # One sender at a time: a double tap must not tell the same people twice.
    if not _claim(rid, row["id"], "changes"):
        return {"ok": False, "in_progress": True, "schedule_id": row["id"],
                "error": "This week is being sent to staff right now."}, 409
    who = _actor_name(actor)
    # What staff had before this send — the published copy — for the
    # predictability-pay record (MARKET-2); covers and swaps since are in it.
    _told_before = _sv.published_rows(row["id"], rid)
    regates = 0
    try:
        while True:
            try:
                stamped = _stamp(rid, row["id"], "changes", who, _sv.authority_of(actor), gate_sig, version=version)
                break
            except _Refused as r:
                if r.payload.get("changed") and version is None and regates < GATE_RETRIES:
                    # A save landed while the gate ran: the changes that go
                    # are the saved ones, checked again (BUGSLIFE-6).
                    regates += 1
                    gate_sig = _rows_sig_now(rid, row["id"])
                    review = publish_review(rid, row["id"])
                    _acked, unacked = _ack_gate(review, acknowledge)
                    if unacked:
                        return _needs_ack(review, unacked, row["id"])
                    continue
                if r.payload.get("already_published"):
                    _vn = _sv.newest_version(rid, row["id"])
                    return dict(r.payload, week_label=week_label, sent=[], unreachable=[], failed=[], sending=[],
                                unsent_changes=[], version=_vn[0]["version"] if _vn else None,
                                status=_models_mod.get_schedule_share_status(rid, row["id"]), error=None), 200
                if r.payload.get("stale"):
                    return (stale_payload(rid, row["id"], version) or r.payload), 409
                return r.payload, r.status
    finally:
        _release(rid, row["id"])
    _after_send(rid, row["id"], stamped["version"], review, _acked, override_reasons, actor, _sv.authority_of(actor))
    _record_pay(rid, row["id"], stamped["rows"], who, before_rows=_told_before)
    got = deliver_notices(restaurant_id=rid, batch=stamped["batch"], budget_seconds=INLINE_SECONDS)
    log_account_event(rid, "schedule_changes_sent", actor,
                      detail=f"week of {_mdy_range(row['week_start'], row['week_start'])}: "
                             f"{len(got['sent'])} of {len(stamped['queued'])} told"
                             + (" — acknowledged blockers" if review["blockers"] else ""))
    note = None
    if not got["sent"] and not got["pending"]:
        note = ("Sent to the staff portal. The messages couldn't be sent just now — Cavnar AI tries again in a few "
                "minutes." if got["failed"] else
                "Sent to the staff portal. Nobody whose shifts changed has the app, agreed to texts, or has "
                "an email address on file, so nothing went out.")
    return dict(ok=True, changes_sent=True, schedule_id=row["id"], week_label=week_label, unsent_changes=[],
                acknowledged=bool(review["blockers"]), portal_only=not got["sent"] and not got["pending"], note=note,
                version=stamped["version"], error=None, **_delivered_payload(rid, row["id"], got)), 200


RESEND_SCOPES = ("missing", "all")


def resend(restaurant_id, schedule_id, actor=None, scope="missing"):
    """Send a week staff already have again (BUGSLIFE-9): `scope`
    "missing" — the people who have not got it (their notice failed, they
    were unreachable then, or none went) — or "all", everyone on it (the
    owner's explicit "Send again to everyone"). Nothing on the week
    changes, so there is no gate; the caller checks SCHEDULE_PUBLISH.
    Returns (payload, status)."""
    import schedule_rows as _srows
    import schedule_versions as _sv
    from time_utils import mdy_range as _mdy_range
    actor = actor or {}
    rid = restaurant_id
    scope = scope if scope in RESEND_SCOPES else "missing"
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, week_start, week_end, published_at, superseded_by, "
                           "COALESCE(published_csv, schedule_csv) AS live FROM schedule_history "
                           "WHERE id=? AND restaurant_id=?", (int(schedule_id), rid)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That week is gone — reload the schedule."}, 404
    if _sv.replaced_error(row):
        return {"ok": False, "replaced": True, "superseded_by": row["superseded_by"], "schedule_id": row["id"],
                "error": _sv.replaced_error(row)}, 409
    if not row["published_at"]:
        return {"ok": False, "error": "This week hasn't been sent to staff yet — send it first."}, 400
    week_label = _mdy_range(row["week_start"], row["week_end"] or row["week_start"])
    from labor import employees_in_schedule
    names = employees_in_schedule(row["live"] or "")
    if scope == "missing":
        told = {_fold(p["employee_name"]) for p in notice_status(rid, row["id"])["people"] if p["status"] == "sent"}
        told |= {_fold(s["employee_name"]) for s in _models_mod.get_schedule_share_status(rid, row["id"])}
        names = [n for n in names if _fold(n) not in told]
    if not names:
        return dict(ok=True, nothing_to_send=True, schedule_id=row["id"], week_label=week_label, sent=[],
                    unreachable=[], failed=[], sending=[], note="Everyone on this week has it.",
                    status=_models_mod.get_schedule_share_status(rid, row["id"]),
                    delivery=notice_status(rid, row["id"]), error=None), 200
    batch = _uuid.uuid4().hex
    who = _actor_name(actor)
    rows = _srows.parse(row["live"] or "")
    person_ids = {" ".join(str(r.get("employee") or "").split()): r.get("person_id") for r in rows
                  if r.get("person_id")}
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        queued = enqueue(conn, rid, row["id"], row["week_start"], batch, [(n, "posted") for n in names],
                         requested_by=who, person_ids=person_ids)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    got = deliver_notices(restaurant_id=rid, batch=batch, budget_seconds=INLINE_SECONDS)
    log_account_event(rid, "schedule_resent", actor,
                      detail=f"week of {_mdy_range(row['week_start'], row['week_start'])}: "
                             f"{'everyone' if scope == 'all' else 'the people who had not got it'} — "
                             f"{len(got['sent'])} of {len(queued)} told")
    return dict(ok=True, resent=True, scope=scope, schedule_id=row["id"], week_label=week_label, error=None,
                **_delivered_payload(rid, row["id"], got)), 200


def edited_since(restaurant_id, schedule_id, version=None, since=None, db_path=None) -> bool:
    """Whether a person changed the week's rows after a queued Send was
    pressed: a version past `version` that changed rows and is not a staff
    swap or a Send's own marker (a cover both people were told about
    changes nothing the sender saw) — or, for a send queued without a
    version (auto-publish, an older row), an edit stamped at or after
    `since`."""
    import schedule_versions as _sv
    conn = get_conn(db_path)
    try:
        if version is not None:
            try:
                base = int(version)
            except (TypeError, ValueError):
                base = 0
            vs = _sv.materialize(conn, conn.execute(
                "SELECT version, reason, " + _sv.VERSION_TEXT + " FROM schedule_versions WHERE history_id=? "
                "AND restaurant_id=? AND version>=? ORDER BY version", (schedule_id, restaurant_id, base)).fetchall(),
                schedule_id)
            if not vs or vs[0]["version"] != base:
                return bool(vs)          # the queued version is not on file: never assume unchanged
            prev = _sv._rows_sig(vs[0]["schedule_csv"])
            for v in vs[1:]:
                cur = _sv._rows_sig(v["schedule_csv"])
                if cur != prev and v["reason"] not in (_sv.LIVE_REASON, "published"):
                    return True
                prev = cur
            return False
        if since:
            r = conn.execute("SELECT edited_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                             (schedule_id, restaurant_id)).fetchone()
            return bool(r and r["edited_at"] and str(r["edited_at"]) >= str(since))
        return False
    finally:
        conn.close()


# The names the publish path had in client_api; re-exported there.
_publish_schedule = publish
send_schedule_changes = send_changes


def _ack_gate(review, acknowledge):
    """(acked_keys, unacked_blockers) for one publish_review. `acknowledge`
    is a list of blocker keys (what the person was shown) or a bool (an
    older client's "yes to whatever the gate finds now")."""
    items = review["blockers"]
    if isinstance(acknowledge, (list, tuple, set)):
        keys = {str(k) for k in acknowledge}
        return [b["key"] for b in items if b["key"] in keys], [b for b in items if b["key"] not in keys]
    if acknowledge:
        return [b["key"] for b in items], []
    return [], list(items)


def _needs_ack(review, unacked, schedule_id):
    # Every blocker a person is shown is kept by key (TRUST-28): the
    # denominator of "you sent past this 9 of 9 times".
    try:
        import schedule_overrides as _so_na
        _rid_na = None
        conn = get_conn()
        try:
            r_ = conn.execute("SELECT restaurant_id FROM schedule_history WHERE id=?", (schedule_id,)).fetchone()
            _rid_na = r_["restaurant_id"] if r_ else None
        finally:
            conn.close()
        if _rid_na:
            _so_na.record_shown(_rid_na, schedule_id, review.get("blockers") or [])
    except Exception as _nax:
        print(f"[publish] shown blockers not recorded for {schedule_id}: {_nax}")
    return {"ok": False, "needs_ack": True, "blockers": [b["text"] for b in review["blockers"]],
            # The keys, parallel to `blockers` (and as {key, text} items):
            # the client sends back `acknowledge: [the keys it showed]` (F2-9).
            "blocker_keys": [b["key"] for b in review["blockers"]],
            "blocker_items": review["blockers"], "new_blockers": [b["text"] for b in unacked],
            # Worth a look, never holding the send (the quality verdict,
            # SQ-29): shown beside the blockers, nothing to acknowledge.
            "notes": review.get("notes") or [],
            # The warnings this owner always sends past (TRUST-28).
            "always_overridden": review.get("always_overridden") or [],
            "schedule_id": schedule_id,
            "error": "This week has things to look at before it goes to staff."}, 409
