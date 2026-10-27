"""
schedule_publish.py — sending a week to staff (-6, -7, -9, -10, -11).

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
   the rows the sender reviewed (`version`) and the gate read; published_at
   and the PUBLISHED copy (published_csv) set from exactly those rows; any
   older published copy of the week retired; the `published` version
   appended; and one outbox row per person to tell (`schedule_notices`) —
   everyone on a first send; on a replacement or a changes send only the
   people whose shifts differ, the people taken off the week included;
4. DELIVERY from the outbox, after the commit: inline for at most
   INLINE_SECONDS, the rest by the `schedule_notices` job; each person's
   result is kept (sent / unreachable / failed), a failure is retried with
   backoff and never counted as told, and "Send again" re-queues the people
   who have not got it — or everyone, when asked.

client_api re-exports the old names (`publish_review`, `publish_blockers`,
`_publish_schedule`, `send_schedule_changes`,...) so callers and tests keep
working.
"""
import json
import logging
import os
import time as _time
import uuid as _uuid
from datetime import datetime as _dt, timedelta as _td

import config
import models as _models_mod
import ops as _ops

log = logging.getLogger(__name__)

from schedule_context import degraded as _degraded  # noqa: E402  (the one degradation event)

# A Send delivers inline for at most this long, so a publish to a large
# staff on email or text never holds a request thread for minutes; the rest
# go out from the outbox job within the minute. 3 s (it was 8 — blind
# re-audit 10/20/26 R5-03: on one web worker's four threads a Sunday of Sends
# each held a thread for its gate plus 8 s of email and text).
INLINE_SECONDS = float(os.getenv("SCHEDULE_SEND_INLINE_SECONDS", "") or 3.0)
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
    notice about the same week replaced it before it went).

    At most once to the provider: `send_started_at` is stamped, over the
    deliverer's own claim, before the provider is called, and a notice whose
    send started under a claim that died (a deploy's SIGTERM, a crash between
    the provider's answer and the outcome's write) is never sent again on
    its own — it is left failed, saying it may have reached them, for the
    owner's Send again (release-safety audit 10/26/26 A02-11: it went out
    a second time when its lease ran out). `provider_id` keeps the
    provider's message id where it gives one (Resend's, for email)."""
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
            send_started_at  TEXT,                  -- stamped before the provider is called (at most once)
            provider_id      TEXT,                  -- the provider's message id, when it gives one (email)
            UNIQUE(batch, employee_name)
        )""")
        # A database made before the send stamp (release-safety audit
        # 10/26/26 A02-11).
        have = {str(r[1]).lower() for r in conn.execute("PRAGMA table_info(schedule_notices)").fetchall()}
        for col in ("send_started_at", "provider_id"):
            if col not in have:
                conn.execute(f"ALTER TABLE schedule_notices ADD COLUMN {col} TEXT")
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
# owner's words (schedule_rules._input_problem): one wording,
# schedule_rules.INPUT_PROBLEM_WORDS, shared with the generation's review.
def _input_problem_text(p) -> str:
    """One blocker line for a setting or source the rules could not read
    (Constraints.input_problems) — schedule_rules.input_problem_text, the
    line the generation's review names it with."""
    import schedule_rules as _sr_ipt
    return _sr_ipt.input_problem_text(p)


def _notice_text(short, week_start, law=None) -> str:
    """The notice-rule blocker. A week already under way said "starts in -2
    days": it says when it started. Where the notice is a jurisdiction's
    law (`law`, its place) the line says so ("your N-day schedule notice
    rule" hid that it was a law)."""
    from time_utils import mdy as _mdy_n
    given = int(short["days_given"])
    when = (f"starts in {given} day{'s' if given != 1 else ''}" if given > 0 else "starts today" if given == 0
            else f"started {-given} day{'s' if given != -1 else ''} ago")
    if law:
        return (f"Less notice than {law} law requires — it says a schedule is posted {short['notice_days']} days "
                f"ahead, and the week of {_mdy_n(week_start)} {when}")
    return (f"Less notice than your own {short['notice_days']}-day schedule notice rule: the week of "
            f"{_mdy_n(week_start)} {when}")


def _law_label(c) -> str:
    """The place whose law the restaurant's rules come from ("Chicago"),
    from the compliance pack — or "your local"."""
    pack = (getattr(c, "compliance", None) or {}).get("_pack") or {}
    labels = [str(x) for x in (pack.get("labels") or []) if x]
    return " and ".join(labels) if labels else (str(pack.get("label") or "").strip() or "your local")


def _notice_legal(short, law) -> dict:
    """What the owner acknowledges to send a week past a posting-notice
    law: the law, what it asks and what is known about the cost. A late
    posting's fine is set by the city and not priced here — said so, never
    a made-up figure; changes after posting are priced by predictability
    pay where the law has it."""
    return {"law": law, "notice_days": int(short["notice_days"]), "days_given": int(short["days_given"]),
            "cost": None,
            "text": (f"I understand {law} law requires posting {int(short['notice_days'])} days ahead, that this "
                     "week goes out late, and that the restaurant may be fined for it. Cavnar AI can't price "
                     "the fine; changes after it's posted owe predictability pay where the law has it.")}


def _live_week(restaurant_id, week_start, not_id):
    """The copy of `week_start` staff have now — its live copy
    (models.live_week_row), with its PUBLISHED rows as `rows` (read from the
    shift table on the same connection — schedule_shifts) — when that is
    another row than `not_id`; else None."""
    import schedule_shifts
    conn = get_conn()
    try:
        live = _models_mod.live_week_row(conn, restaurant_id, week_start)
        if live is None or live["id"] == not_id or not live["published_at"]:
            return None
        live = dict(live)
        live["rows"] = schedule_shifts.rows_of(live["id"], "live", conn=conn)
    finally:
        conn.close()
    return live


def _in_restaurant_dayparts(fn):
    """schedule_engine.in_restaurant_dayparts, read at call time (the gate
    runs inside the restaurant's own lunch/dinner boundary)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        import schedule_engine as _se_dp
        return _se_dp.in_restaurant_dayparts(fn)(*a, **k)
    return wrapper


# Each blocker's tier: MUST_FIX — illegal, the owner's non-negotiable (a
# manager on the floor every minute), or a week nobody could vouch for — is
# acknowledged only by an override of its own with a reason (a manager gap
# only by naming who runs the floor); DECIDE — the owner's own number or a
# choice (the hourly budget, the notice rule, a late change, a week Shift
# Quality could not score, a line of the draft's own) — by one tap; nothing
# else holds a send.
MUST_FIX, DECIDE = "must_fix", "decide"
# Ranks inside MUST_FIX, before any rule's own tier (schedule_rules.TIER_*):
# a roster with no manager at all, then a day nobody drafted, then the checks
# that could not vouch for the week.
_RANK = {"roster": -3, "unwritten": -2, "sweep_failed": -1, "input": 0, "stage": 0, "needs_review": 3,
         # A send that undoes a cover or swap staff were told about.
         "reverts": 4}
# The keys whose override must name who runs the floor.
COVER_KINDS = ("rule:no_manager", "roster:no_manager_roster")
# What each blocker is, by where its rule comes from — the tier and how it
# may be sent past follow:
#   law      a jurisdiction's law or a minor's legal limits: never typed past
#            — fixed, or the week is not sent. The one law no edit can fix
#            (a jurisdiction's posting notice: time has passed) is sent past
#            only by the account owner with an explicit acknowledgement of
#            the law (`legal_ack`) and a reason — never a delegated login,
#            never automation.
#   manager  a manager on the floor every minute: sent past only by naming a
#            real manager or owner on the roster who can work that day, or
#            the account owner reading it — never a free-text name.
#   person   the person can't work it (time off, availability, their own
#            maximum, an overlap): a reason of MIN_REASON words.
#   coverage the floors and closers: a reason.
#   policy   the restaurant's own limits that are not law (days in a row,
#            the longest shift, rest the law doesn't require): a DECISION,
#            one tap — the owner's own number.
#   check    a check that could not vouch for the week: a reason.
LAW, MANAGER, PERSON, COVERAGE, POLICY, CHECK = "law", "manager", "person", "coverage", "policy", "check"
MINOR_KINDS = frozenset({"minor_late", "minor_hours", "minor_early", "minor_week_hours", "minor_school_hours"})
# Who may send past the one law no edit can fix.
LEGAL_ACK_AUTHORITIES = ("principal", "admin")


def breach_class(v) -> str:
    """The class (above) of one hard breach from the sweep."""
    import schedule_rules as _sr_c
    kind = (v or {}).get("kind")
    if (v or {}).get("law") or kind in MINOR_KINDS:
        return LAW
    if kind in ("no_manager",) or kind in _sr_c.ROSTER_BLOCKERS:
        return MANAGER
    t = _sr_c.tier_of(v)
    if t == _sr_c.TIER_POLICY:
        return POLICY
    if t == _sr_c.TIER_COVERAGE:
        return COVERAGE
    return PERSON


def cover_candidates(c, rows, date=None) -> list:
    """Who may be named as running the floor through a manager gap on
    `date`: the roster's managers and owners (Constraints.manages — an
    acting manager that day included), active, and not on time off that
    date. The reader counts too when they are the account owner — added at
    the gate (_override_ok), never here. Display names."""
    if c is None:
        return []
    names, seen = [], set()
    pool = list(getattr(c, "roster_names", None) or []) + list((getattr(c, "display", None) or {}).values())
    for n in pool:
        k = c.key(n) if hasattr(c, "key") else str(n).strip().lower()
        if not n or k in seen or k in (getattr(c, "inactive", None) or set()):
            continue
        seen.add(k)
        try:
            if not c.manages(n, date):
                continue
        except Exception:
            continue
        if date and date in ((getattr(c, "blocked_dates", None) or {}).get(k) or {}):
            continue
        names.append(n)
    return names


def tier_of_key(key) -> str:
    """A blocker's tier from its key, for a blocker built without one:
    schedule_overrides.DECIDE_HEADS are DECIDE, everything else MUST_FIX —
    never the other way round."""
    import schedule_overrides as _so_t
    return _so_t.tier_of_key(key)


def blocker_tier(b) -> str:
    """One blocker's tier: its own, else read from its key."""
    return b.get("tier") or tier_of_key(b.get("key"))


def override_needs(key) -> str:
    """"covered_by" for a manager gap — its override names the person who
    runs the floor (the owner counts) — else "reason"."""
    import schedule_overrides as _so_n
    return "covered_by" if _so_n.kind_of(key) in COVER_KINDS else "reason"


def _unwritten_key(items) -> str:
    return "unwritten:" + ",".join(sorted({str(it.get("date") or "") for it in items or []}))


@_in_restaurant_dayparts
def publish_review(restaurant_id, schedule_id=None, unattended=False, today=None, rows=None, keep=None) -> dict:
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
    breaches, a setting or source the rules could not read, the rule check
    or the quality engine failing to run, the notice rule, a change inside
    the notice window to a week staff already have, and hourly hours past
    the hourly budget. The quality verdict — a weak week, low confidence,
    shifts below the bar — is a note, never a blocker: the scorer's
    structural caps put one on nearly every week, which taught owners to
    acknowledge without reading. Notes ride in `notes` (with keys) and
    `soft`.

    `unattended` is automation (auto-publish, its delayed run): the soft
    flags schedule_rules.HOLD_UNATTENDED names stop it too, and every other
    soft flag is returned in `soft` for the owner's notice. Each blocker
    carries a stable `key` so an acknowledgement given for one set does not
    carry to a blocker that appeared later.

    Each blocker also carries `tier` (MUST_FIX | DECIDE), `rank` (severity
    inside its tier: the law and the manager rule first, never the date) and
    `date`. Rule breaches come only from the live sweep: a saved review line
    is read only for what the sweep cannot recompute — the generation's own
    notes — and a day it could not write is read against the rows as they
    stand (a breach fixed in Team settings stayed a blocker until the week
    was saved again). `held_unattended` lists what would hold an unattended
    send whatever `unattended` is, so one run answers a person's Send and
    the one-tap verdict both.

    `rows` checks the rows on screen instead of the stored ones (the
    pre-publish screen with unsaved edits); `keep`, a dict, is filled
    with what the run read ({"row", "rows", "constraints",
    "violations", "review", "quality"}) for prepublish.review."""
    _ensure_history_columns = _models_mod._ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        cols = ("SELECT id, week_start, week_end, schedule_csv, review_json, review_ref, quality_json, quality_ref, "
                "hours_scheduled, hours_hourly, hours_salaried, hours_budget, economics_json, published_at, "
                "generated_at FROM schedule_history ")
        if schedule_id:
            row = conn.execute(cols + "WHERE id=? AND restaurant_id=?", (int(schedule_id), restaurant_id)).fetchone()
        else:
            # The week in force (models.newest_draft), never simply the
            # newest row — a discarded rewrite or a replaced draft.
            row = conn.execute(cols + "WHERE restaurant_id=? AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
                               (restaurant_id,)).fetchone()
        stored_rows = []
        if row is not None:
            # The week's WORKING rows from the shift table, read beside the
            # row (schedule_shifts).
            import schedule_shifts as _ss_pr
            stored_rows = _ss_pr.rows_of(row["id"], "working", conn=conn) if rows is None else []
            # The whole review and evaluation, whatever is inline.
            import models as _m_sp
            row = dict(row)
            row["review_json"] = json.dumps(_m_sp.history_review(conn, row), default=str)
            row["quality_json"] = json.dumps(_m_sp.history_quality(conn, row), default=str)
    finally:
        conn.close()
    if not row:
        return {"blockers": [], "soft": [], "notes": [], "hours": None, "schedule_id": None, "held_unattended": []}
    out, soft, notes, seen = [], [], [], set()
    held_unattended = []   # what would hold an unattended send
    pp_out = None          # the priced late changes (predictability_pay.preview), when a law covers it

    def add(key, text, tier=MUST_FIX, rank=None, date=None, **meta):
        if text and text not in seen:
            seen.add(text)
            b = {"key": key, "text": text, "tier": tier,
                 "rank": _RANK.get(str(key).split(":", 1)[0], 5) if rank is None else rank, "date": date,
                 # A check that could not vouch for the week, unless the
                 # caller says what it is.
                 "cls": CHECK if tier == MUST_FIX else POLICY, "law": False}
            b.update(meta)
            out.append(b)

    def note(key, text):
        # Acknowledge-able, never holding a publish.
        if text and text not in seen:
            seen.add(text)
            notes.append({"key": key, "text": text})
            soft.append(text)

    import schedule_rules as _sr
    screen = rows is not None
    rows = [dict(r) for r in rows] if screen else stored_rows
    flagged = (sum(1 for r in rows if str(r.get("needs_review") or "").strip().lower() in ("1", "true", "yes")
                   or "NEEDS REVIEW" in str(r.get("notes") or "")) if screen
               else sum(1 for line in (row["schedule_csv"] or "").split("\n") if "NEEDS REVIEW" in line))
    if flagged:
        add("needs_review", f"{flagged} shift{'s' if flagged != 1 else ''} marked NEEDS REVIEW")
    try:
        review = json.loads(row["review_json"] or "null") or {}
    except Exception:
        review = {}
    # A repair stage of a hard rule or the manager rule that did not run on
    # this draft: its breaches were never repaired, so the week is read
    # before it goes out — wherever its line sits in the review.
    stage_lines = set()
    for f in review.get("stage_failures") or []:
        if f.get("blocks_publish"):
            t = str(f.get("line") or "").lstrip("⚠ ").strip()
            stage_lines.add(t)
            add(f"stage:{f.get('stage')}", t or f"A repair step ({f.get('stage')}) didn't run on this draft")
    # A day the generation could not write, or nobody could work, read
    # against the rows as they stand: one the owner has since filled is not
    # said as empty (refresh_review does the same on a save).
    on = {r.get("date") for r in rows if (r.get("employee") or "").strip()}
    said_days = set()
    try:
        import schedule_engine as _se_pr
        for field_, line_of in (("unwritten_dates", _se_pr._unwritten_line),
                                ("unstaffable_dates", _se_pr._unstaffable_line)):
            items = [u for u in (review.get(field_) or []) if isinstance(u, dict)]
            if not items:
                continue
            try:
                said_days.add(line_of(items).lstrip("⚠ ").strip())
            except Exception:
                pass
            left = [u for u in items if u.get("date") not in on]
            if left:
                add(_unwritten_key(left), line_of(left).lstrip("⚠ ").strip(), date=left[0].get("date"))
    except Exception as _ux:
        _degraded(f"unwritten days unreadable: {_ux!r}", _ux)
    # A role the requirements need with nobody of it on all day, read
    # against the rows as they stand: one the owner has since covered is
    # not said.
    try:
        import schedule_shapes as _shp_pr
        items = [u for u in (review.get("uncovered_roles") or []) if isinstance(u, dict)]
        if items:
            said_days.add(_shp_pr.uncovered_line(items).lstrip("⚠ ").strip())
            import shift_quality as _sq_pub
            fam = _sq_pub.role_family
            on_now = {(r.get("date"), fam(r.get("role"))) for r in rows if (r.get("employee") or "").strip()}
            left = [u for u in items if (u.get("date"), fam(u.get("role"))) not in on_now]
            if left:
                add("uncovered:" + ",".join(f"{u['date']}:{fam(u['role'])}" for u in left[:6]),
                    _shp_pr.uncovered_line(left).lstrip("⚠ ").strip(), tier=DECIDE, rank=12,
                    date=left[0].get("date"))
    except Exception as _ucx:
        _degraded(f"uncovered roles unreadable: {_ucx!r}", _ucx)
    # The generation's own lead lines — only what the live sweep cannot
    # recompute: the sweep's rule lines, the budget's, the stage failures,
    # the settings the rules could not read and the empty days above are
    # each read fresh, never from the save. A review saved before its lines
    # were told apart keeps only its lead.
    rule_lines = {x.lstrip("⚠ ").strip() for x in (review.get("rule_lines") or []) + (review.get("budget_lines") or [])
                  if isinstance(x, str)}
    input_lines = set()
    for p in review.get("input_problems") or []:
        try:
            input_lines.add(_input_problem_text(p))
        except Exception:
            pass
    for line in [x for x in (review.get("lead_lines") or []) if isinstance(x, str)]:
        if not line.startswith("⚠") or "over the ceiling" in line:   # the hours check below says it once
            continue
        t = line.lstrip("⚠ ").strip()
        if t in rule_lines or t in stage_lines or t in input_lines or t in said_days:
            continue
        add("saved:" + t, t, tier=DECIDE, rank=40)
    # The sweep against today's data: time off approved, a person
    # deactivated, a floor or a minor's age band set since the draft.
    c = None
    viols = []
    try:
        from datetime import datetime as _dt, timedelta as _td
        ws = _dt.strptime(str(row["week_start"])[:10], "%Y-%m-%d")
        we = _dt.strptime(str(row["week_end"] or row["week_start"])[:10], "%Y-%m-%d")
        dates = [(ws + _td(days=i)).strftime("%Y-%m-%d") for i in range(max(0, min(13, (we - ws).days)) + 1)]
        days = [_dt.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates]
        c = _sr.build_constraints(restaurant_id, dates, days, restaurant=get_restaurant(restaurant_id))
        # A setting or source the rules could not read checked this week
        # without it — a person's managers, minors, hours limits or
        # certificates silently gone. Named, never passed over.
        for p in (getattr(c, "input_problems", None) or []):
            add(f"input:{p.get('source')}:{(p.get('name') or '').strip().lower()}", _input_problem_text(p))
        # The week as it will really run: a holder a manager let off a shift —
        # an approved drop nobody has claimed, a shift posted open before it
        # began — is not on it, so the only manager's approved drop is the
        # manager gap it opens, never a covered week read from the stored
        # rows. Approved time off stays on the row: the sweep names it as the
        # breach it is.
        import shift_requests as _srq_eff
        swept = _srq_eff.effective_rows(restaurant_id, rows, include_time_off=False)
        viols = _sr.violations(swept, c)
        # A maximum is named on every row of the payroll week: one line per
        # person and payroll week (it kept only the first person's, so a
        # second person past their own maximum was never on the list).
        max_seen, hard = set(), []
        for v in viols:
            if not v["hard"]:
                continue
            if v["kind"] == "over_max_hours":
                mk = ((v.get("employee") or "").strip().lower(), v.get("bucket"))
                if mk in max_seen:
                    continue
                max_seen.add(mk)
            hard.append(v)
        breaches = {}
        for v in sorted(hard, key=lambda x: (x.get("date") or "", x.get("employee") or "")):
            # Ranked by the rule's tier — the law and the manager rule first
            # (schedule_rules.tier_of), never by date.
            t = _sr.tier_of(v)
            rank = 10 + (t if t is not None else 9)
            # Tiered by where the rule comes from: the law is never typed
            # past, the restaurant's own limits that are not law are a
            # decision — not the other way round.
            cls = breach_class(v)
            tier = DECIDE if cls == POLICY else MUST_FIX
            meta = {"cls": cls, "law": cls == LAW, "fixable": True}
            # A breach about a day is named on the day, never charged to
            # whoever's row it is pinned to (schedule_rules.breach_text).
            if v.get("day_level"):
                k = f"rule:{v['kind']}:{v.get('date')}:{v.get('gap_start') or v.get('floor_role') or ''}"
                if cls == MANAGER:
                    meta["cover_candidates"] = cover_candidates(c, rows, v.get("date"))
                    if v.get("gap_start") is not None and v.get("gap_end") is not None:
                        # The stretch itself: an override naming a cover puts
                        # that person on it (cover_specs, R5-09).
                        meta["gap"] = [int(v["gap_start"]), int(v["gap_end"])]
                add(k, _sr.breach_text(v), tier=tier, rank=rank, date=v.get("date"), **meta)
                breaches.setdefault(k, v)
                continue
            where = f"{v.get('day') or v.get('date')} {v.get('shift_start') or ''}".strip()
            k = f"rule:{v['kind']}:{(v.get('employee') or '').strip().lower()}:{v.get('date')}:{v.get('shift_start')}"
            if v.get("structural"):
                # A limit the roster cannot keep at all (one manager open
                # every day): named once in setup, here only as a note —
                # never a weekly decision about the engine's own choice.
                note(f"structural:{v['kind']}:{(v.get('employee') or '').strip().lower()}",
                     v.get("structural_text") or _sr.policy_text(v))
                breaches.setdefault(k, v)
                continue
            # The restaurant's own limit, not law, crossed to cover a higher
            # rule reads "over your limit", a decision.
            text = _sr.policy_text(v) if v.get("policy") else f"{v['employee']} — {where}: {v['detail']}"
            add(k, text, tier=tier, rank=rank, date=v.get("date"), **meta)
            breaches.setdefault(k, v)
        if keep is not None:
            keep["breaches"] = breaches
        ceiling_seen = set()
        for v in viols:
            if v["hard"]:
                continue
            if v["kind"] == "over_hours_ceiling":
                # One overtime line per person and payroll week, never one per
                # shift of it (it names every row of the week, like the hard
                # maximum above).
                ck = ((v.get("employee") or "").strip().lower(), v.get("bucket"))
                if ck in ceiling_seen:
                    continue
                ceiling_seen.add(ck)
            if v["kind"] in _sr.ROSTER_BLOCKERS:
                # Nobody counted as a floor manager: the highest rule is unmet
                # on every shift, so the week waits for the owner to name who
                # runs the floor.
                add(f"roster:{v['kind']}", v["detail"][:1].upper() + v["detail"][1:], cls=MANAGER, law=False,
                    fixable=True, cover_candidates=[])
                continue
            where = f"{v.get('day') or v.get('date')} {v.get('shift_start') or ''}".strip()
            text = f"{v['employee']} — {where}: {v['detail']}"
            if v["kind"] in _sr.HOLD_UNATTENDED:
                hk = f"soft:{v['kind']}:{(v.get('employee') or '').strip().lower()}:{v.get('date')}"
                held_unattended.append({"key": hk, "text": text})
                if unattended:
                    add(hk, text, tier=DECIDE, rank=30, date=v.get("date"))
                    continue
            if text not in soft:
                soft.append(text)
        now = today or _local_today(restaurant_id)
        import schedule_versions as _sv_pb
        import predictability_pay as _pp_pb
        live = None if row["published_at"] else _live_week(restaurant_id, row["week_start"], row["id"])
        # What staff have now: the live copy this draft replaces, or this
        # week's own published copy — the base every late change, its
        # predictability pay and the access-to-hours note are read from.
        told = (live["rows"] if live is not None else
                (_sv_pb.published_rows(row["id"], restaurant_id) if row["published_at"] else None))
        if live is not None:
            # A regenerated draft of a week staff already have: what moves
            # is the shifts that differ, and inside the notice window that
            # is the predictability-pay warning — not "the week starts in
            # -2 days" from the notice rule. Where a law covers the
            # restaurant it is priced per changed shift.
            changed = _sv_pb.shift_changes(told, rows)["dates"]
            warn = _sr.late_change_warning(c.compliance, changed, now)
            pp = _pp_pb.preview(restaurant_id, told, rows)
            if pp.get("covered"):
                pp_out = pp
                if pp.get("text"):
                    add("late_change:" + ",".join(changed), "This replaces the week staff already have. " + pp["text"],
                        tier=DECIDE, rank=21)
            elif warn:
                add("late_change:" + ",".join(changed), "This replaces the week staff already have. " + warn,
                    tier=DECIDE, rank=21)
        elif not row["published_at"]:
            short = _sr.notice_shortfall(c.compliance, row["week_start"], now)
            if short:
                # Tiered by where the notice rule comes from: a
                # jurisdiction's posting notice is the law — must-fix, said
                # as the law, sent past only with the owner's legal
                # acknowledgement (no edit can give back the days); the
                # owner's own notice rule is their decision, one tap.
                if _sr.law_backed(c, "notice_days"):
                    add("notice_short", _notice_text(short, row["week_start"], law=_law_label(c)),
                        tier=MUST_FIX, rank=1, cls=LAW, law=True, fixable=False,
                        legal=_notice_legal(short, _law_label(c)))
                else:
                    add("notice_short", _notice_text(short, row["week_start"]), tier=DECIDE, rank=20,
                        cls=POLICY, law=False)
        else:
            # Saved changes to a week staff have, about to be sent.
            pending = _sv_pb.unsent_changes(restaurant_id, row["id"]) or {}
            warn = _sr.late_change_warning(c.compliance, pending.get("dates") or [], now)
            pp = _pp_pb.preview(restaurant_id, told, rows) if told is not None else {"covered": False}
            if pp.get("covered"):
                pp_out = pp
                if pp.get("text"):
                    add("late_change:" + ",".join(pending.get("dates") or []), pp["text"], tier=DECIDE, rank=21)
            elif warn:
                add("late_change:" + ",".join(pending.get("dates") or []), warn, tier=DECIDE, rank=21)
        # The staff changes on the week staff have that this send would undo:
        # a cover, claim or swap that went through after the draft was made,
        # or a manager's working copy still holding the shift as it was. Each
        # is a must-fix line naming it — sent past only with a reason, and the
        # send closes the request and tells both people (_stamp). A
        # replacement that keeps them says which it carried.
        if told is not None:
            import shift_requests as _srq_sc
            sc = _srq_sc.staff_changes_vs(restaurant_id, told, rows, dates=dates)
            for it in sc["reverted"]:
                add(f"reverts:{it['request_id']}", it["text"][:1].upper() + it["text"][1:], rank=_RANK["reverts"],
                    date=it["date"], request_id=it["request_id"])
            if live is not None:
                made = str(row["generated_at"] or "")
                for it in sc["kept"]:
                    if str(it.get("decided_at") or "") >= made:
                        note(f"carried:{it['request_id']}",
                             "Carried from the week staff have, agreed after this draft was made: " + it["text"])
            if keep is not None:
                keep["staff_changes"] = sc
        # Hours added straight to someone's week while a law asks for them to
        # be offered to the current staff first (access to hours): a note,
        # never a hold.
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
        # June 1 to July 31 — said once a year.
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
    # say no week was ever trimmed). It is an HOURLY budget, so the week's
    # hourly hours are held against it, as they stand now: salaried people's
    # hours are never spent from it, and counting them put a spurious "over
    # the ceiling" of about the salaried hours on every week.
    hours = None
    try:
        hours = (_sr.hours_split(rows, c) if c is not None else
                 {"hourly": float(row["hours_hourly"]) if row["hours_hourly"] is not None else
                  float(row["hours_scheduled"] or 0),
                  "salaried": float(row["hours_salaried"] or 0), "total": float(row["hours_scheduled"] or 0)})
        hb = float(row["hours_budget"] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        hb = 0.0
    # Held in dollars when the week can be priced: each person at their own
    # rate, overtime at its premium and an hourly closing manager's assumed
    # stay, against the hourly budget's dollars — the one measure the
    # generation's trim and judge use; hours stay the view beside it.
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
        _degraded(f"dollar budget check unavailable: {_dx!r}", _dx)
        dollars = None
    if dollars is not None:
        cost, lbd = dollars
        if cost > lbd * 1.02:
            hs = (hours or {}).get("hourly") or 0.0
            add("hours_over", f"${cost:,.0f} of hourly pay against a ${lbd:,.0f} hourly budget — ${cost - lbd:,.0f} "
                              f"over ({hs:,.0f}h of hourly shifts against {hb:,.0f}h"
                              + (f"; the {hours['salaried']:,.0f}h salaried aren't counted against it)"
                                 if hours and hours.get("salaried") else ")"),
                tier=DECIDE, rank=25, over={"dollars": round(cost - lbd), "hours": round(hs - hb, 1) if hb else None})
    elif hours and hb > 0 and hours["hourly"] > hb * 1.02:
        hs = hours["hourly"]
        add("hours_over", f"{hs:,.0f}h of hourly shifts against a {hb:,.0f}h hourly budget — {hs - hb:,.0f}h over "
                          "the ceiling" + (f" (the {hours['salaried']:,.0f}h salaried aren't counted against it)"
                                           if hours.get("salaried") else ""),
            tier=DECIDE, rank=25, over={"dollars": None, "hours": round(hs - hb, 1)})
    try:
        quality = json.loads(row["quality_json"] or "null") or {}
    except Exception:
        quality = {}
    if "checked" in quality and not quality.get("checked"):
        # The quality engine failed on this week: said, with the cause, so
        # a week nobody could score never goes out as if it had passed.
        cause = str(quality.get("error") or quality.get("reason") or "").strip()
        add("quality_unchecked", "Shift Quality couldn't check this week" + (f" ({cause[:160]})" if cause else "")
            + " — read it yourself before it goes to staff", tier=DECIDE, rank=24)
    elif quality.get("checked"):
        if quality.get("band") == "weak":
            note("quality_weak", f"Shift Quality {quality.get('score')}/100 — a weak week")
        if (quality.get("confidence") or {}).get("level") == "low":
            note("quality_low", "The quality engine had too little to judge this week on")
        below = quality.get("below_profile") or []
        if below:
            note("below_profile",
                 f"{len(below)} shift{'s' if len(below) != 1 else ''} below the bar set for {'it' if len(below) == 1 else 'them'}")
    # What the owner has sent past before, by key: each blocker's record
    # beside it, the ones they always send past ranked last, a demotable
    # one they always override (the hourly budget, the draft's own review
    # lines — never a rule) moved to a note, and the review of the warnings
    # they always override.
    import schedule_overrides as _so_pr
    # In a week already under way, a problem on a day already worked holds
    # nothing: nobody can be sent for a shift that has happened, so it is one
    # quiet note, never "must fix" (owner, 10/14/26 - a four-day-old draft
    # opened on a Wednesday had Tuesday's shifts at the top of its must-fix
    # list). A week wholly ahead or wholly past is read as before.
    try:
        _t = today or _local_today(restaurant_id)
        _t_iso = _t.isoformat() if hasattr(_t, "isoformat") else str(_t)[:10]
        _ws, _we = str(row.get("week_start") or "")[:10], str(row.get("week_end") or row.get("week_start") or "")[:10]
        underway = bool(_ws) and _ws <= _t_iso <= (_we or _ws)
        past = ([b for b in out if b.get("date") and str(b["date"])[:10] < _t_iso] if underway else [])
        if past:
            out = [b for b in out if b not in past]
            days = sorted({str(b["date"])[:10] for b in past})
            from time_utils import mdy as _mdy_pb
            note("past_days", f"{len(past)} problem{'s' if len(past) != 1 else ''} on "
                              f"{'a day' if len(days) == 1 else 'days'} already worked "
                              f"({', '.join(_mdy_pb(d) for d in days)}) — nothing to send for "
                              f"{'it' if len(days) == 1 else 'them'}, so {'it doesn' if len(past) == 1 else 'they don'}"
                              "'t hold this week")
            held_unattended = [h for h in held_unattended
                               if not any(h.get("key", "").endswith(":" + d) for d in days)]
    except Exception as _px:
        _degraded(f"past-day check unavailable: {_px!r}", _px)
    out.sort(key=lambda b: (b.get("tier") != MUST_FIX, b.get("rank") or 0, b.get("date") or ""))
    if keep is not None:
        keep.update(row=dict(row), rows=rows, constraints=c, violations=viols, review=review, quality=quality,
                    screen=screen)
    return _so_pr.annotate(restaurant_id, {"blockers": out, "soft": soft, "notes": notes, "hours": hours,
                                           "schedule_id": row["id"], "held_unattended": held_unattended,
                                           # Each changed shift inside a predictive-scheduling
                                           # law's notice window, priced, and the total — None
                                           # when no law covers the restaurant or nothing
                                           # changed.
                                           "predictability_pay": (
                                               {k: pp_out[k] for k in ("law", "code", "total", "owed_count",
                                                                       "unknown_count", "text", "rows")}
                                               if pp_out else None)}, unattended=unattended)


def _drop_share(token, schedule_id):
    """Remove a share link made for a send that did not go out, so the
    status list never shows someone as sent who was not. By the token's
    hash — the only form the row keeps."""
    _models_mod.drop_schedule_share(token, schedule_id)


# ── telling one person ──────────────────────────────────────────────────────

_PENDING = object()      # the delivery chain has not answered yet


def _final_item(name, ch, channel, shifts, box=None) -> dict:
    """The outbox item for what a delivery chain finally did (people.deliver's
    on_outcome): "push" — Apple took it; "sms" / "sms_held" / "email" — the
    fallback went; None — nobody was told. `_status` is the notice's status
    (sent | failed)."""
    ch = ch or {}
    if channel == "push":
        return {"_status": "sent", "employee_name": name, "sent_to": "app", "channel": "push",
                "delivery": "delivered", "shifts": shifts}
    if channel in ("sms", "sms_held"):
        return {"_status": "sent", "employee_name": name, "sent_to": ch.get("sms"), "channel": "sms",
                **({"delivery": "held_until_morning"} if channel == "sms_held" else {}), "shifts": shifts}
    if channel == "email":
        return {"_status": "sent", "employee_name": name, "sent_to": (ch.get("email") or "").strip(),
                "channel": "email", "shifts": shifts}
    err = (box or {}).get("email_error")
    if not err:
        err = ("The app notification didn't reach their phone"
               + ("" if (ch.get("sms") or ch.get("email")) else ", and there's no text consent or email on file"))
    return {"_status": "failed", "employee_name": name, "error": err}


def _notice_outcome(notice_id, item, db_path=None):
    """Write a delivery chain's late answer onto its outbox row: a push Apple
    accepted is delivered, a fallback that went is sent on that channel, a
    chain that reached nobody is failed (retried on the outbox's backoff;
    never counted as told). Only a row still sending or sent is touched — a
    notice a newer one cancelled stays cancelled. Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            if item.get("_status") == "sent":
                conn.execute("UPDATE schedule_notices SET status='sent', channel=?, sent_to=?, delivery=?, "
                             "shifts=COALESCE(?, shifts), sent_at=datetime('now'), last_error=NULL, "
                             "next_attempt_at=NULL, claimed_at=NULL WHERE id=? AND status IN ('sending', 'sent')",
                             (item.get("channel"), item.get("sent_to"), item.get("delivery"), item.get("shifts"),
                              notice_id))
            else:
                r = conn.execute("SELECT attempts FROM schedule_notices WHERE id=?", (notice_id,)).fetchone()
                attempts = int((r["attempts"] if r else 1) or 1)
                retry = None
                if attempts < NOTICE_MAX_ATTEMPTS:
                    retry = _utc(_dt.utcnow() + _td(minutes=NOTICE_RETRY_MINUTES[min(attempts, len(NOTICE_RETRY_MINUTES)) - 1]))
                conn.execute("UPDATE schedule_notices SET status='failed', delivery=NULL, sent_at=NULL, last_error=?, "
                             "next_attempt_at=?, claimed_at=NULL, send_started_at=NULL "
                             "WHERE id=? AND status IN ('sending', 'sent')",
                             ((item.get("error") or "not sent")[:300], retry, notice_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _ops.capture(e, job="schedule_notice_outcome", context=f"notice={notice_id}")

def _deliver_one(rid, restaurant, schedule_id, csv_text, name, ch, week_label, kind="posted", base_url=None,
                 notice_id=None, db_path=None):
    """Tell one person about one week, on the channel they signed up with
    (Friction audit #17): the app when their phone has it, a text when they
    ticked "text me when my schedule is posted", email as the fallback —
    people.deliver, the one rule (/M8). Their own share link either way;
    the portal has the week regardless. `kind`: posted | changed | removed
    (taken off the week: "no shifts"). Returns (status, item): "sent" |
    "unreachable" | "failed", and the line the response and the outbox
    keep.

    Only a delivery that happened counts as sent: deliver() never raises —
    a missing key, a suppressed address, the flood guard and a Resend
    failure all come back as SendResult(ok=False) — and only an exception
    used to count as a failure. A share row made for a send that did not go
    out is removed, so the status list never shows that person as sent.
    deliver() records the real outcome in email_log itself.

    A push is "sent" with delivery "pending" when it is queued; what Apple
    (or the fallback chain) answers later is the notice's final word:
    delivered, the text or email the chain sent instead, or failed when the
    chain reached nobody — kept on the outbox row `notice_id`
    (_notice_outcome). A stale device token with no text consent or email
    used to stay "sent", and "Send again to missing" skipped the person."""
    import threading as _threading
    from labor import employee_shifts_from_csv, employee_shifts_from_rows
    import people as _people
    create_schedule_share = _models_mod.create_schedule_share
    ch = ch or {}
    base_url = base_url or config.base_url()
    email = (ch.get("email") or "").strip()
    if not (ch.get("push_user_id") or ch.get("sms") or email):
        return "unreachable", {"employee_name": name, "reason": "no app, text consent or email address on file"}
    # The week's rows (the outbox's context reads them from the shift table —
    # schedule_shifts); the old direct-send shape passes its text.
    shifts = employee_shifts_from_rows(csv_text, name) if isinstance(csv_text, list) else \
        employee_shifts_from_csv(csv_text, name)
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
        box["provider_id"] = getattr(res, "message_id", None)
        return True

    lock = _threading.Lock()

    def _outcome(channel):
        # The app's share row only once the push reached a phone.
        if channel == "push":
            try:
                create_schedule_share(rid, schedule_id, name, sent_to="app")
            except Exception as e:
                _degraded(f"app share not recorded for {name}: {e}", e)
        # The chain's final word: read below when it came before deliver()
        # returned; written to the outbox row when it came after.
        with lock:
            box["final"], late = channel, box.get("returned", False)
        if late and notice_id:
            _notice_outcome(notice_id, _final_item(name, ch, channel, len(shifts), box), db_path=db_path)

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
    with lock:
        box["returned"] = True
        final = box.get("final", _PENDING)
    if how == "push":
        if final is not _PENDING:
            # The chain already ended (a synchronous answer): its outcome,
            # not the queueing, is what happened.
            item = _final_item(name, ch, final, len(shifts), box)
            return item.pop("_status"), item
        # Queued to their phone; "delivery: pending" until Apple answers
        # (the app's share row appears then, or a text/email goes instead,
        # or the notice is failed — _notice_outcome).
        return "sent", {"employee_name": name, "sent_to": "app", "channel": "push", "delivery": "pending",
                        "shifts": len(shifts)}
    if how in ("sms", "sms_held"):
        return "sent", {"employee_name": name, "sent_to": ch["sms"], "channel": "sms",
                        **({"delivery": "held_until_morning"} if how == "sms_held" else {}), "shifts": len(shifts)}
    if how == "email":
        item = {"employee_name": name, "sent_to": email, "channel": "email", "shifts": len(shifts)}
        if box.get("provider_id"):
            item["provider_id"] = box["provider_id"]        # kept on the outbox row, not in the response
        return "sent", item
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
        item.pop("provider_id", None)
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
    by_fold = {}
    for w in waiting:
        by_fold.setdefault(_fold(w["employee_name"]), []).append(w["id"])
    # One statement each for the cancels and the inserts (scale audit
    # 10/19/26 A5-F14): a statement per person — on Postgres a savepoint
    # each — inside the stamp's write held the one writer for a whole staff.
    cancel, rows = [], []
    for name, kind in items:
        name = " ".join(str(name or "").split())
        if not name:
            continue
        cancel += by_fold.get(_fold(name), [])
        pid = (person_ids or {}).get(name)
        rows.append((restaurant_id, history_id, week_start, batch, name, int(pid) if pid else None, kind,
                     (requested_by or "")[:120] or None))
        queued.append((name, kind))
    if cancel:
        conn.executemany("UPDATE schedule_notices SET status='cancelled', next_attempt_at=NULL WHERE id=? "
                         "AND status IN ('pending', 'failed')", [(i,) for i in sorted(set(cancel))])
    if rows:
        conn.executemany("INSERT OR IGNORE INTO schedule_notices (restaurant_id, history_id, week_start, batch, "
                         "employee_name, person_id, kind, requested_by) VALUES (?,?,?,?,?,?,?,?)", rows)
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
        # Claimed NOTICE_CLAIM_CHUNK at a time in one write (scale audit
        # 10/19/26 A5-F14): a claim and a finish per person was ~2 write
        # transactions a notice, ~150 for one 60-person Send, each taking the
        # one writer. A chunk the bound cut short hands back what it did not
        # reach (_unclaim), so the next pass takes it at once.
        rows = list(rows)
        for start in range(0, len(rows), NOTICE_CLAIM_CHUNK):
            if _time.monotonic() - started > budget_seconds or (limit is not None and out["attempted"] >= limit):
                out["hit_bound"] = True
                break
            chunk = rows[start:start + NOTICE_CLAIM_CHUNK]
            if limit is not None:
                chunk = chunk[:max(0, limit - out["attempted"])]
            last_id = chunk[-1]["id"]
            mine = _claim_notices(chunk, db_path)
            for _nid, _name in getattr(mine, "interrupted", ()) or ():
                out["failed"].append({"employee_name": _name, "error": INTERRUPTED_SEND_LINE})
            for pos, n in enumerate(chunk):
                if n["id"] not in mine:
                    continue
                if _time.monotonic() - started > budget_seconds:
                    out["hit_bound"] = True
                    _unclaim([m for m in chunk[pos:] if m["id"] in mine], mine, db_path)
                    break
                _deliver_claimed(n, ctx, out, base_url, db_path, token=mine[n["id"]])
            if out["hit_bound"]:
                break
        if out["hit_bound"]:
            break
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


# Notices claimed together in one write (deliver_notices).
NOTICE_CLAIM_CHUNK = 10
# A Send's notices the inline window did not reach go out from a bulk
# job_queue row, this long a run, queued again while some remain (scale
# audit 10/19/26 A5-F14): the minute duty's 20 s a minute alone left a
# morning of auto-publishes hours behind. The duty still sweeps retries.
NOTICE_QUEUE_SECONDS = 120.0
NOTICE_QUEUE_MAX_ROUNDS = 30


def deliver_inline(restaurant_id, batch) -> dict:
    """A Send's own delivery: INLINE_SECONDS on the request, and what that
    did not reach handed to the queue (queue_delivery)."""
    got = deliver_notices(restaurant_id=restaurant_id, batch=batch, budget_seconds=INLINE_SECONDS)
    if got.get("pending"):
        queue_delivery(restaurant_id, batch)
    return got


def queue_delivery(restaurant_id, batch, round_=0):
    """One bulk `schedule_notices` job_queue row delivering the rest of
    `batch`, keyed so a round is queued once. Never raises: the minute duty
    delivers it all the same."""
    try:
        import job_queue
        job_queue.enqueue("schedule_notices", {"restaurant_id": restaurant_id, "batch": batch, "round": int(round_)},
                          restaurant_id=restaurant_id, priority_class="bulk",
                          idempotency_key=f"schedule_notices:{batch}:{int(round_)}")
    except Exception as e:
        _ops.capture(e, job="schedule_notices", context=f"restaurant_id={restaurant_id} batch={batch} queue")


def run_queued_notices(job):
    """job_queue's handler for `schedule_notices`: deliver the batch for up
    to NOTICE_QUEUE_SECONDS; queued again while notices remain (at most
    NOTICE_QUEUE_MAX_ROUNDS rounds — the duty takes the rest)."""
    p = job.payload
    got = deliver_notices(restaurant_id=p.get("restaurant_id"), batch=p.get("batch"),
                          budget_seconds=NOTICE_QUEUE_SECONDS)
    if got.get("hit_bound") and got.get("pending") and int(p.get("round") or 0) < NOTICE_QUEUE_MAX_ROUNDS:
        queue_delivery(p.get("restaurant_id"), p.get("batch"), int(p.get("round") or 0) + 1)
    return {"attempted": got["attempted"], "sent": len(got["sent"]), "failed": len(got["failed"]),
            "pending": len(got["pending"])}


# What a notice whose send started under a claim that died says: the
# provider may have taken it, so it is not sent again on its own.
INTERRUPTED_SEND_LINE = ("Cavnar AI restarted while sending this, so it may already have reached them — "
                         "it isn't sent again on its own.")


class _Claimed(dict):
    """{id: claim token} of the notices to deliver; `interrupted` the
    [(id, employee_name)] the claim found mid-send and left failed."""
    interrupted = ()


def _claim_notices(chunk, db_path=None) -> dict:
    """Claim the due notices of `chunk` in one write: {id: claim token} of
    the ones this pass took (each claim's token is its own, so two
    deliverers in the same second never read each other's).

    A notice taken over from a claim whose send had already started
    (`send_started_at` — the deliverer died after calling the provider, or
    while) is not delivered again: in the same write it is left failed,
    with no retry, saying it may have reached them (INTERRUPTED_SEND_LINE;
    the owner's Send again is the way to tell them once more). Those are
    the result's `interrupted`."""
    now = _dt.utcnow()
    token = f"{_utc(now)}.{_uuid.uuid4().hex[:8]}"
    ids = [int(n["id"]) for n in chunk]
    if not ids:
        return _Claimed()
    marks = ",".join("?" * len(ids))
    conn = get_conn(db_path)
    try:
        conn.execute(f"UPDATE schedule_notices SET status='sending', claimed_at=?, attempts=attempts+1 "
                     f"WHERE id IN ({marks}) AND {_due_sql('schedule_notices')}",
                     [token] + ids + [_utc(now), _utc(now - _td(minutes=NOTICE_LEASE_MINUTES))])
        got = conn.execute(f"SELECT id, restaurant_id, employee_name, send_started_at FROM schedule_notices "
                           f"WHERE id IN ({marks}) AND status='sending' AND claimed_at=?", ids + [token]).fetchall()
        started = [r for r in got if r["send_started_at"]]
        if started:
            conn.executemany("UPDATE schedule_notices SET status='failed', last_error=?, next_attempt_at=NULL, "
                             "claimed_at=NULL WHERE id=? AND restaurant_id=? AND status='sending' AND claimed_at=?",
                             [(INTERRUPTED_SEND_LINE, int(r["id"]), int(r["restaurant_id"]), token) for r in started])
        conn.commit()
    finally:
        conn.close()
    out = _Claimed({int(r["id"]): token for r in got if not r["send_started_at"]})
    out.interrupted = [(int(r["id"]), r["employee_name"]) for r in started]
    if started:
        log.warning("schedule notices %s were mid-send when their deliverer stopped; not sent again",
                    [i for i, _n in out.interrupted])
    return out


def _unclaim(rows, mine, db_path=None) -> None:
    """Hand claimed notices this pass did not reach back as they were."""
    if not rows:
        return
    conn = get_conn(db_path)
    try:
        conn.executemany("UPDATE schedule_notices SET status=?, claimed_at=NULL, attempts=attempts-1 "
                         "WHERE id=? AND status='sending' AND claimed_at=?",
                         [("failed" if n["status"] == "failed" else "pending", int(n["id"]), mine[int(n["id"])])
                          for n in rows])
        conn.commit()
    finally:
        conn.close()


def _deliver_claimed(n, ctx, out, base_url, db_path=None, token=None) -> None:
    """Tell one claimed notice's person, from the week's published copy,
    and keep what happened (deliver_notices)."""
    import people as _people
    out["attempted"] += 1
    key = (n["restaurant_id"], n["history_id"])
    if key not in ctx:
        ctx[key] = _notice_context(n["restaurant_id"], n["history_id"], db_path)
    c = ctx[key]
    if c is None:
        _finish_notice(n["id"], "cancelled", error="the week is gone or was never sent", db_path=db_path,
                       token=token)
        return
    if n["employee_name"] not in c["reach"]:
        try:
            c["reach"].update(_people.reach(n["restaurant_id"], [n["employee_name"]], purpose="schedule"))
        except Exception as e:
            _ops.capture(e, job="schedule_publish_reach", context=f"restaurant_id={n['restaurant_id']}")
            c["reach"][n["employee_name"]] = {"push_user_id": None, "sms": None, "email": None}
    if not _stamp_send_started(n["restaurant_id"], n["id"], token, db_path):
        # Another deliverer holds the notice now (this claim's lease ran
        # out): it is theirs to send, never a second copy from here.
        out["attempted"] -= 1
        return
    try:
        status, item = _deliver_one(n["restaurant_id"], c["restaurant"], n["history_id"], c["rows"],
                                    n["employee_name"], c["reach"].get(n["employee_name"]), c["week_label"],
                                    kind=n["kind"], base_url=base_url, notice_id=n["id"], db_path=db_path)
    except Exception as e:
        _ops.capture(e, job="schedule_notice", context=f"restaurant_id={n['restaurant_id']} notice={n['id']}")
        status, item = "failed", {"employee_name": n["employee_name"], "error": str(e)[:200]}
    _finish_notice(n["id"], status, item=item, attempts=int(n["attempts"] or 0) + 1, db_path=db_path,
                   token=token)
    item.pop("provider_id", None)
    out[status].append(item)


def _stamp_send_started(restaurant_id, notice_id, token, db_path=None) -> bool:
    """Mark a claimed notice's send as started, over this deliverer's own
    claim (`token`), before the provider is called: whatever happens to this
    process after, the notice is never sent a second time by a pass that
    takes it over (_claim_notices). False when the claim is no longer ours —
    nothing may be sent then. Without a token (a caller with no claim) there
    is nothing to stamp over."""
    if token is None:
        return True
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE schedule_notices SET send_started_at=? WHERE id=? AND restaurant_id=? "
                         "AND status='sending' AND claimed_at=?",
                         (_utc(), int(notice_id), int(restaurant_id), token)).rowcount
        conn.commit()
    finally:
        conn.close()
    return n == 1


def _notice_context(restaurant_id, history_id, db_path=None):
    """What telling anyone about one week needs: the restaurant, the week's
    label and its PUBLISHED copy as it stands now. None when the week is
    gone or was never published."""
    from time_utils import mdy_range as _mdy_range
    import schedule_shifts
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, week_start, week_end, published_at "
                           "FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (history_id, restaurant_id)).fetchone()
        # The copy staff have, from the shift table (schedule_shifts).
        rows = schedule_shifts.rows_of(row["id"], "live", conn=conn) if row else []
    finally:
        conn.close()
    restaurant = _models_mod.get_restaurant(restaurant_id)
    if not row or not row["published_at"] or not restaurant:
        return None
    return {"restaurant": restaurant, "rows": rows, "reach": {},
            "week_label": _mdy_range(row["week_start"], row["week_end"] or row["week_start"])}


def _finish_notice(notice_id, status, item=None, attempts=1, error=None, db_path=None, token=None):
    """Keep one notice's outcome — only over this deliverer's own claim
    (`token`, the claimed_at its claim wrote): a slow deliverer whose lease
    another pass took over must not overwrite the newer outcome (blind
    re-audit 10/20/26 R9-23). The sent branch also yields to a push answer
    that came back first (_notice_outcome)."""
    item = item or {}
    mine, mine_args = "", ()
    if token is not None:
        mine, mine_args = " AND status='sending' AND claimed_at=?", (token,)
    conn = get_conn(db_path)
    try:
        if status == "sent":
            # Only over the claim: a push whose answer came back first
            # (_notice_outcome) already holds its final word.
            conn.execute("UPDATE schedule_notices SET status='sent', channel=?, sent_to=?, delivery=?, shifts=?, "
                         "provider_id=COALESCE(?, provider_id), "
                         "sent_at=datetime('now'), last_error=NULL, next_attempt_at=NULL, claimed_at=NULL "
                         "WHERE id=? AND status='sending'" + mine,
                         (item.get("channel"), item.get("sent_to"), item.get("delivery"), item.get("shifts"),
                          (str(item.get("provider_id"))[:120] if item.get("provider_id") else None),
                          notice_id) + mine_args)
        elif status == "failed":
            retry = None
            if attempts < NOTICE_MAX_ATTEMPTS:
                retry = _utc(_dt.utcnow() + _td(minutes=NOTICE_RETRY_MINUTES[min(attempts, len(NOTICE_RETRY_MINUTES)) - 1]))
            # The provider said no (or nobody was reached): its retry is a
            # fresh send, so the started stamp goes with this attempt.
            conn.execute("UPDATE schedule_notices SET status='failed', last_error=?, next_attempt_at=?, claimed_at=NULL, "
                         "send_started_at=NULL "
                         "WHERE id=?" + mine, ((item.get("error") or error or "not sent")[:300], retry, notice_id)
                         + mine_args)
        else:
            conn.execute("UPDATE schedule_notices SET status=?, last_error=?, next_attempt_at=NULL, claimed_at=NULL "
                         "WHERE id=?" + mine, (status, (item.get("reason") or error or "")[:300] or None, notice_id)
                         + mine_args)
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


def week_copy_ids(restaurant_id, history_id, db_path=None) -> list:
    """Every copy of `history_id`'s week staff were sent, up to and including
    it, oldest first — a replacement publish tells only the people whose
    shifts changed, so "who has it" for the live copy is read across the
    copies before it. [history_id] when it is a draft nobody was sent or its
    week cannot be read."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT week_start, published_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (int(history_id), restaurant_id)).fetchone()
        if not row or not row["published_at"] or not row["week_start"]:
            return [int(history_id)]
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM schedule_history WHERE restaurant_id=? AND week_start=? AND published_at IS NOT NULL "
            "AND id<=? ORDER BY id", (restaurant_id, row["week_start"], int(history_id))).fetchall()]
    finally:
        conn.close()
    return ids or [int(history_id)]


def _confirmed(p) -> bool:
    """A notice that reached the person: sent, and not a push still waiting
    on Apple's answer."""
    return p.get("status") == "sent" and not (p.get("channel") == "push" and p.get("delivery") == "pending")


def notice_status(restaurant_id, history_id, db_path=None) -> dict:
    """Who has been told about this week and how it went, from the outbox:
    {"people": [{employee_name, status, kind, channel, sent_to, delivery,
    error, attempts, next_attempt_at, sent_at, history_id, carried}] (each
    person's newest notice), "sending": n, "failed": n, "unreachable": n}.

    Read across every copy of the week staff were sent (week_copy_ids): a
    replacement publish tells only the people whose shifts changed, so the
    others' notice is the one from the copy before — `carried` True,
    `history_id` that copy's. A push Apple has not answered yet is
    `delivery` "pending"."""
    ids = week_copy_ids(restaurant_id, history_id, db_path=db_path)
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute("SELECT * FROM schedule_notices WHERE restaurant_id=? AND history_id IN (%s) "
                                "AND status <> 'cancelled' ORDER BY id" % ",".join("?" * len(ids)),
                                (restaurant_id, *ids)).fetchall()
        except Exception:
            rows = []
    finally:
        conn.close()
    newest = {}
    for r in rows:
        newest[_fold(r["employee_name"])] = r
    people_ = [{"employee_name": r["employee_name"], "status": r["status"], "kind": r["kind"],
                "channel": r["channel"], "sent_to": r["sent_to"], "delivery": r["delivery"],
                "error": r["last_error"], "attempts": r["attempts"], "next_attempt_at": r["next_attempt_at"],
                "sent_at": r["sent_at"], "history_id": r["history_id"],
                "carried": int(r["history_id"]) != int(history_id)}
               for r in newest.values()]
    return {"people": people_,
            "sending": sum(1 for p in people_ if p["status"] in ("pending", "sending")),
            "failed": sum(1 for p in people_ if p["status"] == "failed"),
            "unreachable": sum(1 for p in people_ if p["status"] == "unreachable")}


def week_share_status(restaurant_id, history_id, db_path=None) -> list:
    """models.get_schedule_share_status across every copy of the week staff
    were sent (week_copy_ids): one entry per person — the newest send's
    channel and time, the first open, every open counted — so a replacement
    copy does not read everyone it left alone as never sent, and nobody's
    opens start again from zero."""
    out = {}
    kw = {"db_path": db_path} if db_path else {}
    for hid in week_copy_ids(restaurant_id, history_id, db_path=db_path):
        for r in _models_mod.get_schedule_share_status(restaurant_id, hid, **kw):
            k = _fold(r.get("employee_name"))
            cur = out.get(k)
            if cur is None:
                out[k] = dict(r)
                continue
            if str(r.get("sent_at") or "") >= str(cur.get("sent_at") or ""):
                cur["sent_to"], cur["sent_at"] = r.get("sent_to"), r.get("sent_at")
            if r.get("viewed_at") and (not cur.get("viewed_at") or r["viewed_at"] < cur["viewed_at"]):
                cur["viewed_at"] = r["viewed_at"]
            cur["view_count"] = (cur.get("view_count") or 0) + (r.get("view_count") or 0)
    return list(out.values())


# ── the stamp ───────────────────────────────────────────────────────────────

class _Refused(Exception):
    """The stamp found the week moved under the Send: nothing was written."""

    def __init__(self, payload, status):
        super().__init__(payload.get("error") or "refused")
        self.payload, self.status = payload, status


def _recipients(old_live, new_rows, restaurant_id=None, db_path=None) -> list:
    """[(name, kind)] to tell when `new_rows` replace what staff have
    (`old_live`, None on a first send): everyone on a first send; else only
    the people whose shifts differ — "removed" for those taken off the
    week, "posted" for those new to it, "changed" for the rest.

    Compared by the PERSON, not the spelling: both copies' names read as
    the one person each means now (people.canonical_names, as
    unsent_changes reads them), so a rename between two Sends is never "you
    have no shifts" to the old spelling and "is posted" to the new one."""
    import schedule_rows as _srows
    from labor import employees_in_rows
    if old_live is None:
        return [(n, "posted") for n in employees_in_rows(new_rows)]
    if restaurant_id is not None:
        try:
            import schedule_versions as _sv_c
            old_live, new_rows = _sv_c.canonical_rows(restaurant_id, [old_live, new_rows], db_path=db_path)
        except Exception as e:
            log.warning("names not read as people for restaurant %s: %s", restaurant_id, e)
    pc = _srows.people_changed(old_live, new_rows)
    return [(n, "removed" if n in pc["removed"] else "posted" if n in pc["added"] else "changed")
            for n in pc["changed"]]


def stale_payload(restaurant_id, schedule_id, version):
    """A Send of rows the sender no longer has on screen: who changed the
    week since `version` and how — the save conflict's shape. None when the
    rows are still the rows of `version`."""
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


def _stamp(restaurant_id, schedule_id, mode, actor_name, authority, gate_sig, version=None, db_path=None,
           covers=None):
    """Publish (mode "publish") or send the changes to (mode "changes")
    one week, in ONE write transaction: re-read under the lock, refuse
    when the copy was replaced or its rows moved since the gate read them
    (or since `version`, the rows the sender reviewed), set the published
    copy from exactly those rows, retire the week's older published copy,
    append the `published` version and queue the notices. Returns {version,
    batch, queued, week_start, rows}; raises _Refused. `covers`
    (cover_specs): the managers named over a manager gap, put on the week —
    working and published copy alike — in the same transaction (R5-09)."""
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
            # out is not what was checked.
            raise _Refused({"ok": False, "changed": True, "schedule_id": schedule_id,
                            "error": "The week changed while it was being checked, so it wasn't sent. "
                                     "Look at it again, then send."}, 409)
        # The rows from the shift table, inside this transaction (schedule_
        # shifts).
        import schedule_shifts as _ss_st
        rows = _ss_st.rows_of(schedule_id, "working", conn=conn)
        # Each manager named over a gap, on the week (judged before the
        # stamp — cover_refusal); a shift of theirs touching it is extended.
        rows, _cover_takers, _added = _with_covers(rows, covers) if covers else (rows, [], [])
        if mode == "publish":
            live = _models_mod.live_week_row(conn, restaurant_id, row["week_start"])
            old_live = _ss_st.rows_of(live["id"], "live", conn=conn) if (live and live["id"] != schedule_id
                                                                        and live["published_at"]) else None
            if old_live is not None:
                # The replacement keeps the ids of the shifts it keeps.
                rows = _srows.carry_ids(rows, old_live, known_ids=[r.get("shift_id") for r in rows])
            text = _srows.to_csv(rows)
        else:
            old_live, _rebuilt = _sv._live_rows_of(conn, schedule_id, restaurant_id, row)
            old_live = _srows.carry_ids(old_live, rows, known_ids=[r.get("shift_id") for r in old_live])
            text = _srows.to_csv(rows)
        # Who to tell, by the PERSON: a rename between the last Send and this
        # one is the same person.
        items = _recipients(old_live, rows, restaurant_id=restaurant_id, db_path=db_path)
        if mode == "changes" and not items:
            raise _Refused({"ok": True, "already_published": True, "schedule_id": schedule_id}, 200)
        # The version first, then the week's text through the one week write
        # (schedule_versions.store_week) — so its shift projection carries
        # this revision. One transaction.
        _row_id, new_version = _sv._insert_version(conn, restaurant_id, schedule_id, "published", text,
                                                   saved_by=actor_name, saved_authority=authority)
        if mode == "publish":
            # Past the gate: an overlap it holds was fixed or sent past with
            # a reason (schedule_shifts' double-booking guard lets it in).
            _sv.store_week(conn, restaurant_id, schedule_id, working=text, published=text,
                           sets=("published_at=datetime('now')", "published_by=?", "publishing_at=NULL"),
                           args=(actor_name,), allow_overlaps=True)
            # One live version of a week: an older published copy is retired,
            # or both fed outcomes, fairness, hours and claims.
            conn.execute("UPDATE schedule_history SET superseded_by=? WHERE restaurant_id=? AND week_start=? "
                         "AND id<>? AND published_at IS NOT NULL AND superseded_by IS NULL",
                         (schedule_id, restaurant_id, row["week_start"], schedule_id))
        else:
            # A cover added at this Send is on the working copy too, so the
            # two copies never disagree about it.
            _sv.store_week(conn, restaurant_id, schedule_id, published=text, working=text if _cover_takers else None,
                           sets=("republished_at=datetime('now')",), allow_overlaps=True)
            # A shift the Studio moved to someone else keeps its floor
            # section now that staff have the change.
            try:
                _models_mod.carry_shift_sections(conn, restaurant_id, old_live, rows)
            except Exception as e:
                if "no such table" not in str(e).lower():
                    raise
        # The live requests on the week's dates follow the copy staff now
        # have, in this same transaction — a retimed shift carries its
        # request, a removed or reassigned one closes it; the requesters
        # are told after the commit (_after_send).
        week_dates = _sv._week_dates(row["week_start"], row["week_end"] or row["week_start"])
        reverted = []
        try:
            import shift_requests as _srq_f
            # A cover or swap staff were told about that this send undoes —
            # sent past the gate's must-fix line with a reason — is closed
            # here, so nothing reads it as covered any more, and both people
            # are told after the commit.
            if old_live is not None:
                reverted = _srq_f.staff_changes_vs(restaurant_id, old_live, rows, dates=week_dates)["reverted"]
                if reverted:
                    _srq_f.close_reverted(conn, restaurant_id, reverted, actor=actor_name)
            _srq_f.follow_week(conn, restaurant_id, rows, dates=week_dates)
        except Exception as e:
            log.warning("requests not followed for week %s: %s", schedule_id, e)
        batch = _uuid.uuid4().hex
        person_ids = {" ".join(str(r.get("employee") or "").split()): r.get("person_id") for r in rows
                      if r.get("person_id")}
        queued = enqueue(conn, restaurant_id, schedule_id, row["week_start"], batch, items,
                         requested_by=actor_name, person_ids=person_ids)
        conn.commit()
        return {"version": new_version, "batch": batch, "queued": queued, "week_start": row["week_start"],
                "rows": rows, "replaced_live": old_live is not None and mode == "publish",
                "reverted": reverted}
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
    """Take the week's Send claim: one Send at a time. A claim older than
    PUBLISH_CLAIM_MINUTES belongs to a Send that died. Returns whether
    this Send holds it."""
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
    Generate waits for it. Never raises (False)."""
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
    it off, the newer draft is the week's draft now: this copy is
    superseded by it, as the generation would have done."""
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
        _degraded(f"implementation not recorded for {rid}: {_rle}", _rle)
    # What the week's first publish says, into the observation log: every
    # change from the original draft by origin (the manager's, Cavnar AI's
    # kept ones, an admin's), and whether a person sent it. Never blocks
    # the publish.
    try:
        import schedule_versions as _sv_obs
        _sv_obs.observe_publish(rid, schedule_id, authority=authority, editor=actor_name)
    except Exception as _obx:
        _ops.capture(_obx, job="schedule_publish_observe", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    # What the restaurant did with the generated week, on the labor_schedule
    # run that wrote it: the share of its shifts that went out untouched.
    # Never blocks the publish.
    try:
        import schedule_versions as _sv_out
        _sv_out.record_week_outcome(rid, schedule_id, actor=actor, published=True)
    except Exception as _wox:
        _degraded(f"schedule outcome not filed for {rid}: {_wox}", _wox)
    # The reviews' and the nightly reports' "+1" asks this week carries,
    # recorded as implemented under it — the loop from a complaint to the
    # person added and what the complaints did next (re-audit CROSSMODULE-10).
    try:
        import staffing_signals as _stsig_pub
        _stsig_pub.record_published(rid, schedule_id,
                                    user_id=actor.get("id") if isinstance(actor, dict) else None)
    except Exception as _spx:
        _degraded(f"staffing asks not recorded for {rid}: {_spx}", _spx)
    # The week's sales projection the schedule was built against, frozen
    # now so it can be scored when the week closes (forecast_log kind
    # revenue_week, insert-once; CA2 #6). Never blocks the publish.
    try:
        import demand as _demand
        _demand.freeze_week_projection(rid, row["week_start"])
    except Exception as _fx:
        _degraded(f"week projection not frozen rid={rid}: {_fx}", _fx)


def _after_send(rid, schedule_id, version, review, acked, override_reasons, actor, authority):
    """What every stamped Send records — none of it can hold the send: the
    warnings it went out past, by key, with the published version and any
    reason (what lets the list learn which ones this owner always
    overrides); and the live requests on the week's dates follow the copy
    staff now have — a retimed shift carries its request, a removed one
    closes it and its requester is told."""
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
    # ladder): staff see firm / likely / tentative, and a changes send marks
    # the committed rows it changed. The nightly schedule_bench pass records
    # any week this missed.
    try:
        import commitment_ladder as _cl_pub
        _cl_pub.ensure(rid, schedule_id)
    except Exception as _clx:
        _ops.capture(_clx, job="commitment_ladder", context=f"restaurant_id={rid} schedule_id={schedule_id}")
    # The week to the POS time clock where the owner turned that on — queued
    # for the pos_schedule_push job, never waited on, never raising.
    import pos_schedule_push as _psp
    _psp.after_publish(rid, schedule_id, actor=actor)


def _record_pay(rid, schedule_id, rows, sent_by, before_rows=None, replacement=False, version=None):
    """On the predictability-pay record: each changed shift inside a
    predictive-scheduling law's notice window, priced, against what staff
    had (`before_rows`: the live copy a replacement retires, or this week's
    published copy before a changes send), and any consented short rest's
    premium. A first send with nothing replaced records only the rest
    premiums. Every row carries `version`, the published version the Send
    stamped, so payroll or an auditor can tie a premium to the copy that
    caused it. Never stops the send."""
    try:
        import predictability_pay as _pp_pub
        if before_rows is not None:
            _pp_pub.record_send(rid, schedule_id, rows, before_rows=before_rows, sent_by=sent_by, version=version)
        _pp_pub.record_rest_premiums(rid, schedule_id, None, rows, sent_by=sent_by, version=version)
    except Exception as _ppx:
        _ops.capture(_ppx, job="predictability_pay_record", context=f"restaurant_id={rid} schedule_id={schedule_id}")


def _actor_name(actor) -> str:
    return (actor.get("username") or actor.get("email") or "automation") if isinstance(actor, dict) else "automation"


def _delivered_payload(rid, schedule_id, got) -> dict:
    """The response's part about who was told: sent / unreachable / failed
    as the Send found them, `sending` for the people still to go (the
    outbox job reaches them within the minute) and the share status."""
    return {"sent": got["sent"], "unreachable": got["unreachable"], "failed": got["failed"],
            "sending": got["pending"], "status": week_share_status(rid, schedule_id),
            "delivery": notice_status(rid, schedule_id)}


def one_tap_ok(restaurant_id, schedule_id, review) -> bool:
    """Whether a drafted week may go out from one tap, judged on `review`
    (a publish_review of it — attended or not: its `held_unattended` are
    what an unattended run adds): nothing the gate would ask about, no soft
    flag or note unread, and somebody it reaches. The rule
    strategy_routes.draft_one_tap_safe and the Send both apply. Never
    raises (False)."""
    try:
        review = review or {}
        if review.get("blockers") or review.get("soft") or review.get("notes") or review.get("held_unattended"):
            return False
        import people
        import schedule_shifts as _ss_ot
        from labor import employees_in_rows as _eir_ot
        reach = people.reach_summary(people.reach(restaurant_id, _eir_ot(_ss_ot.rows_of(
            int(schedule_id), "working", restaurant_id=restaurant_id))))
        return int(reach.get("total") or 0) > 0
    except Exception as e:
        _ops.capture(e, job="draft_one_tap_safe", context=f"restaurant_id={restaurant_id} schedule_id={schedule_id}")
        return False


def one_tap_held(review) -> list:
    """What holds a one-tap Send, as the owner reads it."""
    review = review or {}
    out = [b.get("text") for b in review.get("blockers") or [] if isinstance(b, dict)]
    out += [s for s in review.get("soft") or [] if isinstance(s, str)]
    out += [h.get("text") for h in review.get("held_unattended") or [] if isinstance(h, dict)]
    out += [n.get("text") for n in review.get("notes") or [] if isinstance(n, dict)]
    return [t for t in dict.fromkeys(out) if t]


def one_tap_refusal(review, schedule_id) -> dict:
    """The answer to a one-tap Send the gate holds: the week opens to read."""
    return {"ok": False, "one_tap_refused": True, "schedule_id": schedule_id, "held": one_tap_held(review),
            "error": "This week has something to read before it goes to staff — open it to review and send."}


def publish(restaurant_id, schedule_id=None, actor=None, acknowledge=False, version=None, override_reasons=None,
            one_tap=False):
    """Send a week to staff for the first time — the route, the mobile twin,
    the delayed run and auto-publish. Returns (payload, http_status).
    `actor` is the user dict acting, or delayed.AUTOMATION_ACTOR; `version`
    the version of the rows the sender reviewed (a Send of rows somebody
    changed since is refused with their changes). `override_reasons` ({key:
    reason}) are kept with the keys the send went out past
    (schedule_overrides). `one_tap` — a Send nobody read a sheet for: it
    acknowledges nothing and goes only when one_tap_ok passes on the gate
    run under the claim (else one_tap_refusal, 409).

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
    # draft.
    _replaced = _sv.replaced_error(row)
    if _replaced:
        return {"ok": False, "replaced": True, "superseded_by": row["superseded_by"],
                "schedule_id": row["id"], "error": _replaced}, 409
    schedule_id = row["id"]
    if version is not None and not row["published_at"]:
        stale = stale_payload(rid, schedule_id, version)
        if stale:
            return stale, 409

    # What staff read in the push, the text and the email subject: M/D/YY,
    # never the ISO the row stores.
    week_label = _mdy_range(row["week_start"], row["week_end"] or row["week_start"])

    # Claim the week before anything is written — and before the gate (scale
    # audit 10/19/26 A5-F14): two managers pressing Send both paid the 10-40 s
    # gate before one of them lost the claim. Nothing stopped a second
    # publish (a double tap, a second device, the 11am auto-publish after a
    # 10am manual one) from emailing every member of staff again.
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
                        status=week_share_status(rid, schedule_id),
                        error=None), 200
        if state and state["superseded_by"]:
            return {"ok": False, "replaced": True, "superseded_by": state["superseded_by"],
                    "schedule_id": schedule_id, "error": _sv.replaced_error(state)}, 409
        return {"ok": False, "in_progress": True, "schedule_id": schedule_id,
                "error": "This week is being sent to staff right now."}, 409

    # `acknowledge` is True (a person read the list just now) or the list
    # of blocker keys a person acknowledged when they queued it (a delayed
    # publish): a blocker that appeared since is not covered (NS5 H3). The
    # gate runs once, under the claim; a refusal lets the claim go.
    unattended = acknowledge is False and bool(actor.get("role") == "automation")
    try:
        gate_sig = _sv._rows_sig(row["schedule_csv"])
        review = publish_review(rid, schedule_id, unattended=unattended)
        if one_tap and not one_tap_ok(rid, schedule_id, review):
            # A one-tap Send nobody read a sheet for goes only when the
            # unattended gate passes (re-audit 10/8/26 #1) — checked here,
            # on the same run of the gate the Send uses (A5-F14: it was run
            # once for the verdict and again for the Send).
            _release(rid, schedule_id)
            return one_tap_refusal(review, schedule_id), 409
        blockers = [b["text"] for b in review["blockers"]]
        _acked, unacked = _ack_gate(review, acknowledge, override_reasons, actor=actor)
    except BaseException:
        _release(rid, schedule_id)
        raise
    if unacked:
        _release(rid, schedule_id)
        # Each blocker with its key, so the client acknowledges exactly the
        # list it showed.
        return _needs_ack(review, unacked, schedule_id)

    actor_name = _actor_name(actor)
    authority = _sv.authority_of(actor)
    # The copy this one replaces, read before the stamp retires it: its
    # changes go on the predictability-pay record.
    _replacing = _live_week(rid, row["week_start"], schedule_id)
    regates = 0
    while True:
        try:
            covers = cover_specs(review, override_reasons)
            refused = cover_refusal(rid, schedule_id, covers)
            if refused:
                raise _Refused(refused, 409)
            stamped = _stamp(rid, schedule_id, "publish", actor_name, authority, gate_sig, version=version,
                             covers=covers)
            break
        except _Refused as r:
            if r.payload.get("changed") and version is None and regates < GATE_RETRIES:
                # A save landed while the gate ran: what goes out is the week
                # as saved — checked again, the claim still held, never the
                # rows read before the save.
                regates += 1
                gate_sig = _rows_sig_now(rid, schedule_id)
                review = publish_review(rid, schedule_id, unattended=unattended)
                blockers = [b["text"] for b in review["blockers"]]
                _acked, unacked = _ack_gate(review, acknowledge, override_reasons, actor=actor)
                if unacked:
                    _release(rid, schedule_id)
                    return _needs_ack(review, unacked, schedule_id)
                continue
            _release(rid, schedule_id)
            if r.payload.get("already_published"):
                return dict(r.payload, week_label=week_label, sent=[], unreachable=[], failed=[], sending=[],
                            status=week_share_status(rid, schedule_id), error=None), 200
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
                before_rows=_replacing["rows"] if _replacing is not None else None,
                replacement=True, version=stamped["version"])
    import schedule_rows as _srows
    _after_publish(rid, schedule_id, dict(row, schedule_csv=_srows.to_csv(stamped["rows"])), actor, actor_name,
                   authority)
    got = deliver_inline(rid, stamped["batch"])
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
                version=stamped["version"], error=None,
                # The covers and swaps this send undid, past the gate's line
                # with a reason: each closed, both people told.
                staff_changes_undone=[it["text"] for it in stamped.get("reverted") or []],
                **_delivered_payload(rid, schedule_id, got)), 200


def send_changes(restaurant_id, schedule_id, actor=None, acknowledge=False, version=None, override_reasons=None):
    """Tell the people whose shifts changed on a week staff already have —
    the explicit second step after a save. Returns (payload, status).

    A save only records the edit on the working copy; this runs the publish
    gate, then — in one transaction — sets the published copy to the saved
    week (the version the sender reviewed), appends a `published` version
    and queues a notice for each person whose shifts differ from what they
    were told, the people taken off the week included. The caller checks
    SCHEDULE_PUBLISH and the undo window
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
    # Changes to a copy a newer one replaced reach nobody's live week.
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
                    status=week_share_status(rid, row["id"]), error=None), 200
    if version is not None:
        stale = stale_payload(rid, row["id"], version)
        if stale:
            return stale, 409
    # One sender at a time: a double tap must not tell the same people twice
    # — claimed before the gate, so the second press never pays for it
    # (scale audit 10/19/26 A5-F14).
    if not _claim(rid, row["id"], "changes"):
        return {"ok": False, "in_progress": True, "schedule_id": row["id"],
                "error": "This week is being sent to staff right now."}, 409
    try:
        gate_sig = _sv._rows_sig(row["schedule_csv"])
        review = publish_review(rid, row["id"])
        _acked, unacked = _ack_gate(review, acknowledge, override_reasons, actor=actor)
    except BaseException:
        _release(rid, row["id"])
        raise
    if unacked:
        _release(rid, row["id"])
        return _needs_ack(review, unacked, row["id"])
    who = _actor_name(actor)
    # What staff had before this send — the published copy — for the
    # predictability-pay record; covers and swaps since are in it.
    _told_before = _sv.published_rows(row["id"], rid)
    regates = 0
    try:
        while True:
            try:
                covers = cover_specs(review, override_reasons)
                refused = cover_refusal(rid, row["id"], covers)
                if refused:
                    raise _Refused(refused, 409)
                stamped = _stamp(rid, row["id"], "changes", who, _sv.authority_of(actor), gate_sig, version=version,
                                 covers=covers)
                break
            except _Refused as r:
                if r.payload.get("changed") and version is None and regates < GATE_RETRIES:
                    # A save landed while the gate ran: the changes that go
                    # are the saved ones, checked again.
                    regates += 1
                    gate_sig = _rows_sig_now(rid, row["id"])
                    review = publish_review(rid, row["id"])
                    _acked, unacked = _ack_gate(review, acknowledge, override_reasons, actor=actor)
                    if unacked:
                        return _needs_ack(review, unacked, row["id"])
                    continue
                if r.payload.get("already_published"):
                    _vn = _sv.newest_version(rid, row["id"])
                    return dict(r.payload, week_label=week_label, sent=[], unreachable=[], failed=[], sending=[],
                                unsent_changes=[], version=_vn[0]["version"] if _vn else None,
                                status=week_share_status(rid, row["id"]), error=None), 200
                if r.payload.get("stale"):
                    return (stale_payload(rid, row["id"], version) or r.payload), 409
                return r.payload, r.status
    finally:
        _release(rid, row["id"])
    _after_send(rid, row["id"], stamped["version"], review, _acked, override_reasons, actor, _sv.authority_of(actor))
    _record_pay(rid, row["id"], stamped["rows"], who, before_rows=_told_before, version=stamped["version"])
    got = deliver_inline(rid, stamped["batch"])
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
                version=stamped["version"], error=None,
                staff_changes_undone=[it["text"] for it in stamped.get("reverted") or []],
                **_delivered_payload(rid, row["id"], got)), 200


# ── one live change, sent on its own (-5) ──
#
# Live labor's "cut Maria at 9" saved the new end through the Studio save and
# then sent the WHOLE week's changes: every unsent Studio edit of any day went
# out with it, past the owner's undo window; and under a notice rule or a
# predictive-scheduling law (Chicago, where Simple EJ's is) the gate refused
# it, so the cut lived only in the working copy — the person was never told,
# the POS and attendance still expected the old end, and leaving early as
# instructed read as left_early. A live change is the employer's change made
# knowingly, now: only its own shifts go to the published copy, its late-
# change premium is acknowledged and on the record, and it reaches only the
# people it moves.

def _live_target(old_live, working, only) -> list:
    """The copy staff have (`old_live`) with only the shifts `only` names
    (shift ids) taken from the working copy: changed, added, or — gone from
    the working copy — removed. Every other shift stays as staff have it."""
    import schedule_rows as _srows
    want = {str(x).lower() for x in only or []}
    work_by = {sid: r for sid, r in _srows.by_id(working).items() if sid in want}
    out, seen = [], set()
    for r in old_live:
        sid = str(r.get("shift_id") or "").lower()
        if sid in want:
            seen.add(sid)
            if sid in work_by:
                out.append(dict(work_by[sid]))
            continue
        out.append(dict(r))
    out += [dict(r) for sid, r in work_by.items() if sid not in seen]
    return out


def send_live_change(restaurant_id, schedule_id, shift_ids, actor=None, version=None, why="live labor"):
    """Send one live change of a week staff have — the shifts `shift_ids`
    name, as the working copy holds them now — and nothing else. Returns
    (payload, status).

    No whole-week gate: the change was judged by the one legality engine when
    it was made (shift_edits.plan) and is judged again here as the change it
    is (schedule_rules.judge_change on the copy staff have — a new or worse
    hard breach refuses it, nothing written). Its late-change warning and
    predictability pay are acknowledged as the employer's knowing change —
    kept with the send (schedule_overrides) and the premium on the ledger
    with the version (predictability_pay.record_send) — never a refusal that
    leaves it unsent. `version` is the working copy's version it was saved
    at: a person's edit since voids it (edited_since). The caller applies the
    owner's undo window (labor_live.act queues it through delayed, like any
    Send)."""
    import schedule_rows as _srows
    import schedule_versions as _sv
    import schedule_rules as _rules
    from time_utils import mdy_range as _mdy_range
    actor = actor or {}
    rid = restaurant_id
    ids = [str(x).lower() for x in shift_ids or [] if _srows.valid_id(x)]
    if not ids:
        return {"ok": False, "error": "Which shift?"}, 400
    if not get_restaurant(rid):
        return {"ok": False, "error": "Restaurant not found"}, 404
    import schedule_shifts as _ss_lc
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, week_start, week_end, schedule_csv, published_csv, published_at, superseded_by "
                           "FROM schedule_history WHERE id=? AND restaurant_id=?", (int(schedule_id), rid)).fetchone()
        # The working rows, read beside the row (schedule_shifts).
        working = _ss_lc.rows_of(row["id"], "working", conn=conn) if row else []
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That week is gone — reload the schedule."}, 404
    if _sv.replaced_error(row):
        return {"ok": False, "replaced": True, "superseded_by": row["superseded_by"], "schedule_id": row["id"],
                "error": _sv.replaced_error(row)}, 409
    if not row["published_at"]:
        return {"ok": False, "error": "This week hasn't been sent to staff yet — send the whole week."}, 400
    if version is not None and edited_since(rid, row["id"], version=version):
        return {"ok": False, "stale": True, "schedule_id": row["id"],
                "error": "The week was changed after this was saved, so it wasn't sent — look at it, then send."}, 409
    week_label = _mdy_range(row["week_start"], row["week_end"] or row["week_start"])
    # Judged as the change it is, on the copy staff have, outside the lock.
    conn = get_conn()
    try:
        old_live, _rebuilt = _sv._live_rows_of(conn, row["id"], rid, row)
    finally:
        conn.close()
    old_live = _srows.carry_ids(old_live, working, known_ids=[r.get("shift_id") for r in old_live])
    target = _live_target(old_live, working, ids)
    if _sv.same_rows(old_live, target):
        return {"ok": True, "already_published": True, "schedule_id": row["id"], "week_label": week_label,
                "sent": [], "unreachable": [], "failed": [], "sending": [], "error": None}, 200
    try:
        import shift_requests as _srq_lc
        worse = _rules.judge_change(old_live, target, _srq_lc._week_constraints(rid, target))
    except Exception as e:
        _ops.capture(e, job="live_change_judge", context=f"restaurant_id={rid} schedule_id={row['id']}")
        return {"ok": False, "schedule_id": row["id"],
                "error": "The rules couldn't be checked just now, so it wasn't sent — try again."}, 503
    if worse:
        return {"ok": False, "illegal": True, "schedule_id": row["id"],
                "problems": [w.get("label") for w in worse][:4],
                "error": "That change breaks a rule on the week staff have: " + (worse[0].get("label") or "a hard rule")}, 409
    # What it may owe, acknowledged with the send.
    late = None
    try:
        import predictability_pay as _pp_lc
        pv = _pp_lc.preview(rid, old_live, target)
        if pv.get("covered") and pv.get("text"):
            late = {"text": pv["text"], "total": pv.get("total"), "law": pv.get("law")}
        else:
            changed = _sv.shift_changes(old_live, target)["dates"]
            w = _rules.late_change_warning(_rules.compliance(rid), changed, _local_today(rid))
            if w:
                late = {"text": w, "total": None, "law": None}
    except Exception as e:
        _ops.capture(e, job="live_change_premium", context=f"restaurant_id={rid}")
    if not _claim(rid, row["id"], "changes"):
        return {"ok": False, "in_progress": True, "schedule_id": row["id"],
                "error": "This week is being sent to staff right now."}, 409
    who = _actor_name(actor)
    try:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            fresh = conn.execute("SELECT schedule_csv, published_csv, published_at, superseded_by FROM schedule_history "
                                 "WHERE id=? AND restaurant_id=?", (row["id"], rid)).fetchone()
            if fresh is None or _sv.replaced_error(fresh) or \
                    _sv._rows_sig(fresh["schedule_csv"]) != _sv._rows_sig(row["schedule_csv"]) or \
                    (fresh["published_csv"] or "") != (row["published_csv"] or ""):
                conn.rollback()
                return {"ok": False, "changed": True, "schedule_id": row["id"],
                        "error": "The week changed while this was being checked, so it wasn't sent — try again."}, 409
            text = _srows.to_csv(target)
            # Through the one week write (store_week: the shift projection
            # with it).
            _sv.store_week(conn, rid, row["id"], published=text, sets=("republished_at=datetime('now')",))
            try:
                _models_mod.carry_shift_sections(conn, rid, old_live, target)
            except Exception as e:
                if "no such table" not in str(e).lower():
                    raise
            import shift_requests as _srq_f
            _srq_f.follow_week(conn, rid, target, dates=_sv._week_dates(row["week_start"],
                                                                        row["week_end"] or row["week_start"]))
            items = _recipients(old_live, target, restaurant_id=rid)
            batch = _uuid.uuid4().hex
            person_ids = {" ".join(str(r.get("employee") or "").split()): r.get("person_id") for r in target
                          if r.get("person_id")}
            queued = enqueue(conn, rid, row["id"], row["week_start"], batch, items, requested_by=who,
                             person_ids=person_ids)
            version_now = _sv.latest_version(conn, row["id"])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        _release(rid, row["id"])
    # The late change, kept as sent past knowingly (the record).
    late_key = "late_change:" + ",".join(_sv.shift_changes(old_live, target)["dates"])
    if late:
        try:
            import schedule_overrides as _so_lc
            _so_lc.record_overridden(rid, row["id"], version_now, [{"key": late_key, "text": late["text"],
                                                                   "tier": DECIDE}], [late_key],
                                     reasons={late_key: f"A {why} change the manager made on the day"},
                                     actor=actor if isinstance(actor, dict) else None,
                                     authority=_sv.authority_of(actor))
        except Exception as e:
            _ops.capture(e, job="schedule_overrides", context=f"restaurant_id={rid} live change")
    _after_send(rid, row["id"], version_now, {"blockers": []}, [], None, actor, _sv.authority_of(actor))
    _record_pay(rid, row["id"], target, who, before_rows=old_live, version=version_now)
    got = deliver_inline(rid, batch)
    log_account_event(rid, "schedule_live_change_sent", actor,
                      detail=f"week of {_mdy_range(row['week_start'], row['week_start'])} ({why}): "
                             f"{len(got['sent'])} of {len(queued)} told")
    return dict(ok=True, live_change_sent=True, schedule_id=row["id"], week_label=week_label,
                late_change=late, version=version_now, error=None,
                portal_only=not got["sent"] and not got["pending"],
                **_delivered_payload(rid, row["id"], got)), 200


RESEND_SCOPES = ("missing", "all")


def resend(restaurant_id, schedule_id, actor=None, scope="missing"):
    """Send a week staff already have again: `scope` "missing" — the
    people who have not got it (their notice failed, they were
    unreachable then, or none went) — or "all", everyone on it (the
    owner's explicit "Send again to everyone"). Nothing on the week
    changes, so there is no gate; the caller checks SCHEDULE_PUBLISH.
    Returns (payload, status)."""
    import schedule_versions as _sv
    from time_utils import mdy_range as _mdy_range
    actor = actor or {}
    rid = restaurant_id
    scope = scope if scope in RESEND_SCOPES else "missing"
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, week_start, week_end, published_at, superseded_by "
                           "FROM schedule_history WHERE id=? AND restaurant_id=?", (int(schedule_id), rid)).fetchone()
        # The copy staff have, from the shift table (schedule_shifts).
        import schedule_shifts as _ss_rs
        rows = _ss_rs.rows_of(row["id"], "live", conn=conn) if row else []
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
    from labor import employees_in_rows
    names = employees_in_rows(rows)
    if scope == "missing":
        # Only a delivery that happened: a push still waiting on Apple's
        # answer is not "has it"; and across every copy of the week staff
        # were sent (a replacement tells only the people whose shifts
        # changed, the rest have it from the copy before).
        told = {_fold(p["employee_name"]) for p in notice_status(rid, row["id"])["people"] if _confirmed(p)}
        told |= {_fold(s["employee_name"]) for s in week_share_status(rid, row["id"])}
        names = [n for n in names if _fold(n) not in told]
    if not names:
        return dict(ok=True, nothing_to_send=True, schedule_id=row["id"], week_label=week_label, sent=[],
                    unreachable=[], failed=[], sending=[], note="Everyone on this week has it.",
                    status=week_share_status(rid, row["id"]),
                    delivery=notice_status(rid, row["id"]), error=None), 200
    batch = _uuid.uuid4().hex
    who = _actor_name(actor)
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
    got = deliver_inline(rid, batch)
    log_account_event(rid, "schedule_resent", actor,
                      detail=f"week of {_mdy_range(row['week_start'], row['week_start'])}: "
                             f"{'everyone' if scope == 'all' else 'the people who had not got it'} — "
                             f"{len(got['sent'])} of {len(queued)} told")
    return dict(ok=True, resent=True, scope=scope, schedule_id=row["id"], week_label=week_label, error=None,
                **_delivered_payload(rid, row["id"], got)), 200


# The versions that are not a person's edit of the rows: a staff cover or
# swap (schedule_versions.LIVE_REASON), a Send's own marker, a roster rename.
NOT_AN_EDIT = ("swap", "published", "rename")


def edited_since(restaurant_id, schedule_id, version=None, since=None, db_path=None) -> bool:
    """Whether a person changed the week's rows after a queued Send was
    pressed: a version past `version` that changed rows and is not a staff
    swap, a Send's own marker or a roster rename (a cover both people were
    told about changes nothing the sender saw; a rename changes a spelling,
    not a shift — rows_changed_since reads it the same way) — or, for a send
    queued without a version (auto-publish, an older row), an edit stamped at
    or after `since` (a rename never stamps edited_at)."""
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
                if cur != prev and v["reason"] not in NOT_AN_EDIT:
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


def cover_specs(review, override_reasons) -> list:
    """[{date, start, end, name}] — each manager gap sent past by naming a
    manager on the roster (an override's `covered_by` that is one of the
    blocker's `cover_candidates`): the named person is put on the week
    across the gap as it goes out (blind re-audit 10/20/26 R5-09), so the
    copy staff read has a manager in charge for that stretch and the cover
    is told like any changed shift. The account owner covering it
    themselves ("owner", "me") is not a roster person: no row is added."""
    out = []
    reasons = override_reasons or {}
    for b in (review or {}).get("blockers") or []:
        gap = b.get("gap")
        o = reasons.get(b.get("key"))
        if not gap or not isinstance(o, dict) or override_needs(b.get("key")) != "covered_by":
            continue
        named = " ".join(str(o.get("covered_by") or "").split())
        match = next((n for n in (b.get("cover_candidates") or [])
                      if " ".join(str(n).split()).casefold() == named.casefold()), None)
        if match and b.get("date"):
            out.append({"date": str(b["date"])[:10], "start": int(gap[0]), "end": int(gap[1]), "name": match,
                        "candidates": list(b.get("cover_candidates") or [])})
    return out


COVER_NOTE = "Running the floor (manager gap covered at Send)"


def _with_covers(rows, covers):
    """(rows, takers, added) — `rows` with each cover (cover_specs) on the
    week: the named person across the gap, in their own manager role of the
    week when they have one. A shift of theirs that day which overlaps or
    touches the gap is EXTENDED over it (two of them that straddle it become
    one) instead of a second, overlapping row staff would be told about
    (blind re-audit 10/26/26 B2); nothing changes where one already spans
    the whole gap. `takers` [(index, name)] are the rows the covers put the
    person on (for schedule_rules.judge_change), `added` the new rows."""
    import uuid as _uuid_c
    import schedule_time as _stime_c
    import schedule_rules as _sr_c
    from datetime import date as _date_c
    out = [dict(r) for r in rows]
    gone, added, touched = set(), [], []
    for cv in covers or []:
        key = " ".join(cv["name"].split()).casefold()
        mine = [i for i, r in enumerate(out) if i not in gone
                and " ".join(str(r.get("employee") or "").split()).casefold() == key]
        day_rows = []
        for i in mine:
            if str(out[i].get("date") or "")[:10] != cv["date"]:
                continue
            sp = _stime_c.span(out[i])
            if sp:
                day_rows.append((i, sp))
        if any(sp[0] <= cv["start"] and sp[1] >= cv["end"] for _i, sp in day_rows):
            continue
        near = [(i, sp) for i, sp in day_rows if sp[0] <= cv["end"] and sp[1] >= cv["start"]]
        if near:
            lo = min([cv["start"]] + [sp[0] for _i, sp in near])
            hi = max([cv["end"]] + [sp[1] for _i, sp in near])
            keep = near[0][0]
            r = out[keep]
            r["shift_start"] = _stime_c.fmt_minutes(lo % (24 * 60))
            r["shift_end"] = _stime_c.fmt_minutes(hi % (24 * 60))
            r["scheduled_hours"] = str(round((hi - lo) / 60.0, 2))
            r["notes"] = ((r.get("notes") or "").strip() + " · " if (r.get("notes") or "").strip() else "") + COVER_NOTE
            gone |= {i for i, _sp in near[1:]}
            touched.append(r)
            continue
        role = next((out[i].get("role") for i in mine
                     if _sr_c.manager_role_kind(out[i].get("role")) in ("manager", "owner")), None) or \
            next((out[i].get("role") for i in mine if out[i].get("role")), None) or "Manager"
        try:
            day = _date_c.fromisoformat(cv["date"]).strftime("%A")
        except ValueError:
            day = ""
        new = {"date": cv["date"], "day": day, "employee": cv["name"], "role": role,
               "shift_start": _stime_c.fmt_minutes(cv["start"] % (24 * 60)),
               "shift_end": _stime_c.fmt_minutes(cv["end"] % (24 * 60)),
               "scheduled_hours": str(round((cv["end"] - cv["start"]) / 60.0, 2)),
               "notes": COVER_NOTE, "shift_id": _uuid_c.uuid4().hex}
        added.append(new)
        touched.append(new)
    final = [r for i, r in enumerate(out) if i not in gone] + added
    takers = [(i, r["employee"]) for i, r in enumerate(final) if any(r is t for t in touched)]
    return final, takers, added


def _cover_rows(rows, covers) -> list:
    """The rows `covers` (cover_specs) add to `rows` as new shifts
    (_with_covers; a shift of theirs that touches the gap is extended
    instead, and is not among these)."""
    return _with_covers(rows, covers)[2]


def cover_refusal(restaurant_id, schedule_id, covers):
    """None, or the 409 payload refusing a Send whose named cover breaks a
    rule (blind re-audit 10/26/26 B2): a manager named to run the floor
    through a gap is put on the week like any other assignment, so they are
    held to the same checks — their availability, part-day time off, rest,
    hours, overlaps, minors (schedule_rules.judge_change with them as the
    taker) — and to the overtime rule: never into overtime while another
    manager who could be named has the room. Read with no lock held, on
    the rows the gate checked (the stamp refuses rows that changed since)."""
    if not covers:
        return None
    import schedule_rules as _sr_c
    import schedule_engine as _se_c
    import schedule_shifts as _ss_c
    from datetime import date as _dc
    from time_utils import mdy
    rows = _ss_c.rows_of(schedule_id, "working")

    def _refused(text):
        return {"ok": False, "cover_refused": True, "schedule_id": schedule_id, "error": text}
    try:
        dates = sorted({r["date"] for r in rows if r.get("date")})
        c = _sr_c.build_constraints(restaurant_id, dates,
                                    [_dc.fromisoformat(d).strftime("%A") for d in dates])
        before = _sr_c.breach_profile(rows, c)

        def _check(cv):
            after, takers, _a = _with_covers(rows, [cv])
            worse = _sr_c.judge_change(rows, after, c, takers=takers, before=before) if takers else []
            if worse:
                return _se_c._breach_reason(worse[0]), 0.0
            extra = 0.0
            for i, nm in takers:
                extra = max(extra, _sr_c.overtime_added(c, nm, after[i], [r for j, r in enumerate(after) if j != i]))
            return None, extra
        for cv in covers:
            when = f"{_dc.fromisoformat(cv['date']).strftime('%a')} {mdy(cv['date'])} " \
                   f"{_sr_c._fmt_minutes(cv['start'] % 1440)} to {_sr_c._fmt_minutes(cv['end'] % 1440)}"
            why, extra = _check(cv)
            if why:
                return _refused(f"{cv['name']} can't run the floor {when}: {why} — name another manager, or add "
                                "a manager's shift that covers it.")
            if extra > 0.05:
                def _clean_cover(n):
                    w, x = _check(dict(cv, name=n))
                    return w is None and x <= 0.05
                other = next((n for n in cv.get("candidates") or []
                              if " ".join(str(n).split()).casefold() != " ".join(cv["name"].split()).casefold()
                              and _clean_cover(n)), None)
                if other:
                    return _refused(f"Covering {when} puts {cv['name']} {extra:g}h into overtime while {other} "
                                    f"can cover it without — name {other}, or add a manager's shift that covers it.")
        if len(covers) > 1:
            after, takers, _a = _with_covers(rows, covers)
            worse = _sr_c.judge_change(rows, after, c, takers=takers, before=before) if takers else []
            if worse:
                return _refused(f"The managers named to cover the gaps can't all be on: {_se_c._breach_reason(worse[0])} "
                                "— name someone else for one of them.")
    except Exception as e:
        import ops
        ops.capture(e, job="schedule_publish", context=f"restaurant_id={restaurant_id} history={schedule_id} cover check",
                    kind="request")
        return _refused("We couldn't check the manager named to cover the gap just now — try again in a minute.")
    return None


def override_policy(b) -> dict:
    """How one MUST-FIX blocker may be sent past: {allowed, needs ("reason" |
    "covered_by" | "legal_ack" | None), why} — a law that an edit can fix
    never (fix it, or the week does not go); the posting-notice law only with
    the owner's legal acknowledgement and a reason; a manager gap by naming a
    real manager or owner; anything else with a reason."""
    if b.get("law"):
        if b.get("fixable", True):
            return {"allowed": False, "needs": None,
                    "why": "This breaks the law — fix it, or the week can't go to staff."}
        return {"allowed": True, "needs": "legal_ack",
                "why": "Only the account owner can send this, acknowledging the law."}
    if override_needs(b["key"]) == "covered_by":
        return {"allowed": True, "needs": "covered_by", "why": None}
    return {"allowed": True, "needs": "reason", "why": None}


def _reader(actor) -> dict:
    """{names (casefolded), authority} of whoever is sending — the person
    behind a queued Send included (delayed.AUTOMATION_ACTOR's on_behalf_of)."""
    import schedule_versions as _sv_r
    a = actor if isinstance(actor, dict) else {}
    names = set()
    for src in (a, a.get("on_behalf_of") if isinstance(a.get("on_behalf_of"), dict) else {}):
        for k in ("name", "username", "email", "display_name"):
            v = " ".join(str(src.get(k) or "").split()).casefold()
            if v:
                names.add(v)
    return {"names": names, "authority": _sv_r.authority_of(a) if a else "system"}


def override_problem(b, reasons, actor=None):
    """Why a MUST-FIX blocker's override is not enough, in the owner's words —
    None when it carries the override of its own the gate asks for (/5/8): a
    reason of a few words (schedule_overrides.reason_problem, never "x"); a
    manager gap names a real manager or owner on the roster who can work that
    day (`cover_candidates`), or the account owner sending it; the
    posting-notice law the owner's explicit `legal_ack`; a law an edit can
    fix, nothing."""
    import schedule_overrides as _so_ok
    pol = override_policy(b)
    if not pol["allowed"]:
        return pol["why"]
    o = (reasons or {}).get(b["key"])
    if isinstance(o, dict):
        reason, covered = str(o.get("reason") or "").strip(), " ".join(str(o.get("covered_by") or "").split())
        legal = o.get("legal_ack") is True
    else:
        reason, covered, legal = str(o or "").strip(), "", False
    if o is None:
        return "Fix it, or override it on its own."
    if pol["needs"] == "covered_by":
        if not covered:
            return "Name who runs the floor through it."
        who = _reader(actor)
        ok = {" ".join(str(n).split()).casefold() for n in (b.get("cover_candidates") or [])}
        import shift_requests as _srq_o
        support = _srq_o.acting_admin(actor)
        if who["authority"] == "principal" and not support:
            # The account owner sending it runs the floor themselves — by
            # their name, or "Owner"/"me" (the override row keeps the actor).
            # Never support or an admin acting through view-as (blind
            # re-audit 10/26/26 B2): "me" from them put nobody on the floor —
            # they name a manager on the roster.
            ok |= who["names"] | {"owner", "the owner", "me", "myself"}
        if covered.casefold() not in ok:
            if (support or who["authority"] == "admin") and \
                    covered.casefold() in (who["names"] | {"owner", "the owner", "me", "myself"}):
                return "Name the manager who runs the floor — support can't cover a shift for the restaurant."
            return (f"{covered} isn't a manager or owner on the roster who can work that day — pick one, or add "
                    "a manager's shift that covers it.")
        return None
    if pol["needs"] == "legal_ack":
        if _reader(actor)["authority"] not in LEGAL_ACK_AUTHORITIES:
            return "Only the account owner can send a week past the law."
        if not legal:
            return "Acknowledge the law to send it anyway."
    return _so_ok.reason_problem(reason)


def _override_ok(b, reasons, actor=None) -> bool:
    """Whether a MUST-FIX blocker carries the override of its own the gate
    asks for (override_problem is None)."""
    return override_problem(b, reasons, actor) is None


def _ack_gate(review, acknowledge, reasons=None, actor=None):
    """(acked_keys, unacked_blockers) for one publish_review. `acknowledge`
    is a list of blocker keys (what the person was shown) or a bool (an
    older client's "yes to whatever the gate finds now").

    Tiered: a DECIDE blocker is acknowledged by its key (or the bool); a
    MUST-FIX one only by an override of its own in `reasons` ({key: reason
    | {reason, covered_by}}) — never by a bare `true` or a key list alone,
    so a manager gap and "2% over budget" no longer share one
    acknowledgement. A blocker from before the tiers (no `tier`) is read as
    must-fix.

    A must-fix override is
    checked, not just present (override_problem): `actor` is who is
    sending — a manager gap may name them when they are the account owner,
    and only they may send past the posting-notice law. Each refused
    override's reason rides on its blocker as `override_problem`."""
    items = review["blockers"]
    reasons = reasons or {}
    if isinstance(acknowledge, (list, tuple, set)):
        keys = {str(k) for k in acknowledge}
        listed = lambda b: b["key"] in keys or b["key"] in reasons    # noqa: E731
    elif acknowledge:
        listed = lambda b: True                                         # noqa: E731
    else:
        listed = lambda b: b["key"] in reasons                          # noqa: E731
    acked, unacked = [], []
    for b in items:
        must = blocker_tier(b) == MUST_FIX
        if not listed(b):
            unacked.append(b)
            continue
        why = override_problem(b, reasons, actor) if must else None
        if why is None:
            acked.append(b["key"])
        else:
            if b["key"] in reasons:
                b["override_problem"] = why
            unacked.append(b)
    return acked, unacked


def _needs_ack(review, unacked, schedule_id):
    # Every blocker a person is shown is kept by key: the
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
        _degraded(f"shown blockers not recorded for {schedule_id}: {_nax}", _nax)
    return {"ok": False, "needs_ack": True, "blockers": [b["text"] for b in review["blockers"]],
            # The keys, parallel to `blockers` (and as {key, text} items): the
            # client sends back `acknowledge: [the keys it showed]`.
            "blocker_keys": [b["key"] for b in review["blockers"]],
            "blocker_items": review["blockers"], "new_blockers": [b["text"] for b in unacked],
            # Worth a look, never holding the send (the quality
            # verdict): shown beside the blockers, nothing to
            # acknowledge.
            "notes": review.get("notes") or [],
            # The warnings this owner always sends past.
            "always_overridden": review.get("always_overridden") or [],
            # The tiers: each must-fix key needs its own override with a
            # reason — a manager gap names who runs the floor — and is
            # never covered by a key list or `true`.
            "must_fix_keys": [b["key"] for b in review["blockers"] if blocker_tier(b) == MUST_FIX],
            "needs_reason": [{"key": b["key"], "needs": override_needs(b["key"])} for b in unacked
                             if blocker_tier(b) == MUST_FIX and override_policy(b)["allowed"]],
            # The publish gate: the must-fix keys
            # no override sends past (a law an edit can fix), and each
            # override given that was refused, with why ("x" is not a
            # reason; a name that is not a manager on the roster).
            "not_overridable": [b["key"] for b in unacked
                                if blocker_tier(b) == MUST_FIX and not override_policy(b)["allowed"]],
            "override_refused": [{"key": b["key"], "why": b["override_problem"]} for b in unacked
                                 if b.get("override_problem")],
            "schedule_id": schedule_id,
            "error": "This week has things to look at before it goes to staff."}, 409
