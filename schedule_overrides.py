"""
schedule_overrides.py — which publish warnings the owner sends past, kept
by key, and what the warning list learns from it (schedule audit 10/10/26
TRUST-28).

client_api._publish_schedule used to compute the acknowledged blocker keys
and keep only the account event's "— acknowledged blockers": nothing could
measure "overridden 9 of 9 times", show the owner "you sent past this last
week", or stop asking about a warning the owner always overrides.

  record_shown(rid, history_id, blockers)       each warning a person was
                                                shown before a send (the 409's
                                                list, or the list a send went
                                                out past)
  record_overridden(rid, history_id, version,   the keys a send went out past,
                    keys, reasons, ...)         with the published version and
                                                any reason given (the I1
                                                publish screen POSTs
                                                {schedule_id, overrides:
                                                [{key, reason}]})
  stats(rid)                                    per kind: weeks shown, weeks
                                                sent past it, the rate and its
                                                Wilson bound, the last time
  annotate(rid, review)                         the publish review with each
                                                blocker's record beside it,
                                                ranked (the ones owners always
                                                override last), a DEMOTABLE
                                                kind owners always override
                                                moved to a note — never a
                                                hard rule — and the
                                                `always_overridden` review

A warning's KIND is its key's stable head: "rule:<kind>", "input:<source>",
"stage:<stage>", "roster:<kind>", "soft:<kind>", "saved" (a line the
generation's own review wrote), "hours_over", "late_change", "notice_short",
"quality_unchecked", "needs_review", "sweep_failed".

Only DEMOTABLE kinds may stop holding a send: the hourly budget's ceiling
(the owner's own number, which they may choose to spend past) and the
generation's own review lines. A rule breach, a source the rules could not
read, a repair stage that did not run, the notice and predictability-pay
warnings, a week nobody could score and the managers' roster gap are hard:
however often they are overridden they keep holding the send — the record is
shown beside them, never used to quiet them. Nothing is demoted for an
unattended send (the automation's gate is unchanged).
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

WINDOW_DAYS = 180
DEMOTABLE_KINDS = ("hours_over", "saved")
# A demotable kind stops holding a send once it was sent past in at least
# this many weeks, and the Wilson bound of the weeks sent past over the
# weeks shown is at least DEMOTE_MIN_CONFIDENCE (9 of 9 clears it; 4 of 5
# does not).
DEMOTE_MIN_WEEKS = 4
DEMOTE_MIN_CONFIDENCE = 0.6
# The `always_overridden` review lists a kind from here (any kind, hard ones
# included — said, never quieted).
ALWAYS_MIN_WEEKS = 3
ALWAYS_MIN_RATE = 0.8

LABELS = {"hours_over": "Hours over the hourly budget", "saved": "A warning from the draft's own review",
          "late_change": "A change inside the notice window", "notice_short": "Sent with less notice than the rule",
          "quality_unchecked": "Shift Quality couldn't check the week", "needs_review": "Shifts marked NEEDS REVIEW",
          "sweep_failed": "The rule check couldn't run"}


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_schedule_overrides(db_path=None):
    """Boot DDL (models.init_db). One row per week and warning key: when a
    person was first shown it, and — when a send went out past it — the
    published version, by whom, with what authority and reason."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_publish_overrides (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            history_id     INTEGER NOT NULL,
            key            TEXT    NOT NULL,
            kind           TEXT    NOT NULL,
            text           TEXT,
            shown_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            overridden_at  TEXT,
            version        INTEGER,
            actor          TEXT,
            authority      TEXT,
            reason         TEXT,
            UNIQUE(restaurant_id, history_id, key)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_overrides_kind "
                     "ON schedule_publish_overrides(restaurant_id, kind, shown_at)")
        conn.commit()
    finally:
        conn.close()


def kind_of(key) -> str:
    """The warning's stable kind from its key (module doc)."""
    k = str(key or "")
    head, _sep, rest = k.partition(":")
    if head in ("rule", "input", "stage", "roster", "soft"):
        return f"{head}:{rest.split(':', 1)[0]}" if rest else head
    return head or "unknown"


def hard(kind) -> bool:
    """Whether a kind must keep holding a send however often it is
    overridden — every kind but DEMOTABLE_KINDS."""
    return str(kind or "") not in DEMOTABLE_KINDS


def label(kind) -> str:
    k = str(kind or "")
    if k in LABELS:
        return LABELS[k]
    head, _sep, rest = k.partition(":")
    words = rest.replace("_", " ") if rest else head
    return {"rule": f"Rule: {words}", "input": f"A setting the rules couldn't read ({words})",
            "stage": f"A repair step that didn't run ({words})", "roster": f"Roster: {words}",
            "soft": f"Held for automation: {words}"}.get(head, words.capitalize())


def record_shown(restaurant_id, history_id, blockers, db_path=None) -> int:
    """Keep each blocker (a {key, text}) as shown on this week — once per
    week and key. Never raises into the publish."""
    rows = [(restaurant_id, int(history_id), str(b.get("key"))[:300], kind_of(b.get("key")),
             str(b.get("text") or "")[:300]) for b in (blockers or []) if b and b.get("key")]
    if not rows or not history_id:
        return 0
    try:
        conn = get_conn(db_path)
        try:
            conn.executemany("INSERT OR IGNORE INTO schedule_publish_overrides (restaurant_id, history_id, key, kind, "
                             "text) VALUES (?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()
        return len(rows)
    except Exception as e:
        _capture(e, "record_shown", restaurant_id)
        return 0


def record_overridden(restaurant_id, history_id, version, blockers, acked_keys, reasons=None, actor=None,
                      authority=None, db_path=None) -> int:
    """Keep the keys a send went out past, with the published version, who
    sent it, their authority and any reason (`reasons` {key: text}). The
    blockers are recorded as shown too. Never raises into the publish."""
    acked = {str(k) for k in (acked_keys or [])}
    if not acked or not history_id:
        return 0
    record_shown(restaurant_id, history_id, [b for b in blockers or [] if str(b.get("key")) in acked], db_path)
    reasons = reasons or {}
    who = ((actor or {}).get("username") or (actor or {}).get("email") or "") if isinstance(actor, dict) else ""
    try:
        conn = get_conn(db_path)
        try:
            for k in acked:
                conn.execute("UPDATE schedule_publish_overrides SET overridden_at=COALESCE(overridden_at, "
                             "datetime('now')), version=COALESCE(?, version), actor=?, authority=?, "
                             "reason=COALESCE(?, reason) WHERE restaurant_id=? AND history_id=? AND key=?",
                             (version, who[:120] or None, authority, (str(reasons.get(k) or "").strip()[:300] or None),
                              restaurant_id, int(history_id), k[:300]))
            conn.commit()
        finally:
            conn.close()
        return len(acked)
    except Exception as e:
        _capture(e, "record_overridden", restaurant_id)
        return 0


def stats(restaurant_id, days=WINDOW_DAYS, db_path=None) -> dict:
    """{kind: {kind, label, hard, shown, overridden, rate, confidence,
    last_overridden (M/D/YY), last_reason, demoted}} over the last `days` —
    counted in WEEKS (a warning shown on one week is one, however many
    people or dates it named)."""
    import learning_stats
    from time_utils import mdy
    since = (datetime.utcnow() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT kind, history_id, MAX(CASE WHEN overridden_at IS NOT NULL THEN 1 ELSE 0 END) AS over, "
                "MAX(overridden_at) AS last, MAX(CASE WHEN reason IS NOT NULL THEN reason END) AS reason "
                "FROM schedule_publish_overrides WHERE restaurant_id=? AND shown_at >= ? "
                "GROUP BY kind, history_id", (restaurant_id, since)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("publish overrides unreadable for %s: %s", restaurant_id, e)
        return {}
    out = {}
    for r in rows:
        e = out.setdefault(r["kind"], {"kind": r["kind"], "label": label(r["kind"]), "hard": hard(r["kind"]),
                                       "shown": 0, "overridden": 0, "last": None, "last_reason": None})
        e["shown"] += 1
        if r["over"]:
            e["overridden"] += 1
            if r["last"] and (e["last"] is None or r["last"] > e["last"]):
                e["last"], e["last_reason"] = r["last"], r["reason"]
    for e in out.values():
        e["rate"] = round(e["overridden"] / float(e["shown"]), 3) if e["shown"] else None
        e["confidence"] = round(learning_stats.wilson_lower(e["overridden"], e["shown"]), 3)
        e["demoted"] = (not e["hard"] and e["overridden"] >= DEMOTE_MIN_WEEKS
                        and e["confidence"] >= DEMOTE_MIN_CONFIDENCE)
        e["last_overridden"] = mdy(e.pop("last")) if e.get("last") else None
    return out


def annotate(restaurant_id, review, unattended=False, db_path=None) -> dict:
    """The publish review (client_api.publish_review's dict), learned from:
    each blocker carries `kind` and `overridden_before` ({weeks, of, last}
    — "you sent past this last week"); the blockers are ranked so the ones
    owners always send past come last; a DEMOTABLE kind owners always
    override (stats' `demoted`) moves from the blockers to the notes, saying
    so — never a hard one, never for an unattended send; and
    `always_overridden` lists every kind sent past in ALWAYS_MIN_RATE of at
    least ALWAYS_MIN_WEEKS weeks, for the owner's review. Never raises."""
    try:
        st = stats(restaurant_id, db_path=db_path)
    except Exception as e:
        log.warning("override stats failed for %s: %s", restaurant_id, e)
        st = {}
    blockers, notes = [], list(review.get("notes") or [])
    soft = list(review.get("soft") or [])
    for b in review.get("blockers") or []:
        kind = kind_of(b.get("key"))
        s = st.get(kind)
        b = dict(b, kind=kind, hard=hard(kind),
                 overridden_before=({"weeks": s["overridden"], "of": s["shown"], "last": s["last_overridden"]}
                                    if s and s["overridden"] else None))
        if s and s.get("demoted") and not unattended:
            text = (f"{b.get('text')} — you've sent past this in {s['overridden']} of {s['shown']} weeks, so it "
                    f"no longer holds the send")
            notes.append({"key": b.get("key"), "text": text, "demoted": True, "kind": kind})
            soft.append(text)
            continue
        blockers.append(b)
    blockers.sort(key=lambda b: (((st.get(b["kind"]) or {}).get("rate") or 0.0), not b["hard"]))
    always = sorted((dict(s) for s in st.values()
                     if s["overridden"] >= ALWAYS_MIN_WEEKS and (s["rate"] or 0) >= ALWAYS_MIN_RATE),
                    key=lambda s: (-(s["rate"] or 0), -s["overridden"], s["kind"]))
    return dict(review, blockers=blockers, notes=notes, soft=soft, always_overridden=always)


def _capture(e, what, restaurant_id):
    log.warning("schedule_overrides %s failed for %s: %s", what, restaurant_id, e)
    try:
        import ops
        ops.capture(e, job="schedule_overrides", context=f"{what} restaurant_id={restaurant_id}",
                    restaurant_id=restaurant_id)
    except Exception:
        pass


def reasons_from(payload) -> dict:
    """{key: reason} from a request's `overrides: [{key, reason}]` (the
    pre-publish screen's contract)."""
    out = {}
    for o in (payload or []) if isinstance(payload, (list, tuple)) else []:
        if isinstance(o, dict) and o.get("key"):
            out[str(o["key"])[:300]] = str(o.get("reason") or "").strip()[:300]
    return out
