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
"stage:<stage>", "roster:<kind>", "soft:<kind>", "saved:<line class>" (a line
the generation's own review wrote, by its normalised wording — saved_class:
numbers, times and dates out — never one bucket for every line, schedule
re-audit 10/11/26 TRUST-9, BUGSLIFE-12), "hours_over", "late_change",
"notice_short", "quality_unchecked", "needs_review", "sweep_failed".

Only DEMOTABLE kinds may stop holding a send: the hourly budget's ceiling
(the owner's own number, which they may choose to spend past) and the
generation's own review lines. A rule breach, a source the rules could not
read, a repair stage that did not run, the notice and predictability-pay
warnings, a week nobody could score and the managers' roster gap are hard:
however often they are overridden they keep holding the send — the record is
shown beside them, never used to quiet them. Nothing is demoted for an
unattended send (the automation's gate is unchanged).

Demotion is by CONTENT and MAGNITUDE (TRUST-9): a saved line only quiets
lines of its own class, and the hourly budget's overage only while it is
within the largest overage the owner sent past (DEMOTE_MAGNITUDE_SLACK over
it) — a $4,000 overage still holds the send after six $40 ones. "Shown" is
recorded wherever a person is shown the warning — the pre-publish review
renders it (prepublish.review), the 409 lists it, a send goes out past it —
so a week the owner FIXED counts against the rate (BUGSLIFE-12).

An override's reason is checked (reason_problem): at least
MIN_REASON_CHARS characters and MIN_REASON_WORDS words — "x" is not a
reason (TRUST-4).

The owner's "not this week" / "keep it" answers to the review's suggestions
(a regular left off, a floor kept) are kept by key in
`schedule_review_dismissals` (dismiss / dismissals, TRUST-17): a week's
answer until the week ends, a standing one until the evidence behind the
suggestion changes (its `evidence` signature) or DISMISS_MAX_DAYS pass.
"""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

WINDOW_DAYS = 180
# The kinds' HEADS that may be demoted ("saved:<class>" is a saved line's
# kind; each class is learned on its own).
DEMOTABLE_KINDS = ("hours_over", "saved")
# Demoted only while the warning's size is within the largest one the owner
# sent past, plus this share of it (TRUST-9).
DEMOTE_MAGNITUDE_SLACK = 0.10
# An override's reason (TRUST-4): "x" is not one.
MIN_REASON_CHARS = 8
MIN_REASON_WORDS = 2
# The longest a standing "keep it" answer quiets a suggestion (TRUST-17).
DISMISS_MAX_DAYS = 180
DISMISS_SCOPES = ("week", "every")
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
            covered_by     TEXT,
            UNIQUE(restaurant_id, history_id, key)
        )""")
        # Who runs the floor through a manager gap the send went out past
        # (schedule audit 10/10/26 ARCH-30) — typed, kept with the version.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_publish_overrides)").fetchall()}
        if "covered_by" not in cols:
            conn.execute("ALTER TABLE schedule_publish_overrides ADD COLUMN covered_by TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_overrides_kind "
                     "ON schedule_publish_overrides(restaurant_id, kind, shown_at)")
        # The retention sweep deletes by shown_at alone (ops._RETENTION_DAYS),
        # which the (restaurant_id, kind, shown_at) index cannot serve.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_publish_overrides_shown "
                     "ON schedule_publish_overrides(shown_at)")
        # How big the warning was when shown / sent past (TRUST-9): the
        # hourly budget's overage in dollars ("$") or hours ("h").
        for col, typ in (("magnitude", "REAL"), ("magnitude_unit", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE schedule_publish_overrides ADD COLUMN {col} {typ}")
        # The owner's answers to the review's suggestions (TRUST-17).
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_review_dismissals (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            key            TEXT    NOT NULL,
            scope          TEXT    NOT NULL DEFAULT 'week',
            history_id     INTEGER,
            until_date     TEXT,
            evidence       TEXT,
            answer         TEXT,
            actor          TEXT,
            authority      TEXT,
            dismissed_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, key)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_review_dismissals_at "
                     "ON schedule_review_dismissals(dismissed_at)")
        conn.commit()
    finally:
        conn.close()


# The publish gate's DECIDE heads (schedule audit 10/10/26 TRUST-3): the
# hourly budget, the notice rule, a late change, a week Shift Quality could
# not score, a line of the draft's own, a flag held for automation. Every
# other blocker is MUST_FIX (schedule_publish.MUST_FIX).
DECIDE_HEADS = ("hours_over", "notice_short", "late_change", "quality_unchecked", "saved", "soft")


def tier_of_key(key) -> str:
    """"decide" for a DECIDE head, else "must_fix" — a blocker's tier
    read from its key when it was built without one."""
    return "decide" if str(key or "").split(":", 1)[0] in DECIDE_HEADS else "must_fix"


_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:\s*(?:am|pm|h|%|min))?|\d{1,2}/\d{1,2}(?:/\d{2,4})?")


def saved_class(text) -> str:
    """A saved line's class: its wording casefolded with the numbers, money,
    times and dates taken out and the punctuation folded — "3 shifts nobody
    could work were left out" and "5 shifts …" are one class; a line about
    an uncovered day and one about the budget never are (TRUST-9). Short
    and readable, with a hash of the whole so two long lines never
    collide."""
    t = _NUM.sub("#", str(text or "").casefold())
    t = re.sub(r"[^a-z#' ]+", " ", t)
    words = [w for w in t.split() if w]
    slug = "_".join(words[:8])[:60] or "line"
    digest = hashlib.sha1(" ".join(words).encode("utf-8")).hexdigest()[:8]
    return f"{slug}~{digest}"


def kind_of(key) -> str:
    """The warning's stable kind from its key (module doc)."""
    k = str(key or "")
    head, _sep, rest = k.partition(":")
    if head in ("rule", "input", "stage", "roster", "soft"):
        return f"{head}:{rest.split(':', 1)[0]}" if rest else head
    if head == "saved":
        return f"saved:{saved_class(rest)}"
    return head or "unknown"


def hard(kind) -> bool:
    """Whether a kind must keep holding a send however often it is
    overridden — every kind but DEMOTABLE_KINDS (by its head: each saved
    line class is demotable, on its own record)."""
    return str(kind or "").split(":", 1)[0] not in DEMOTABLE_KINDS


def reason_problem(text):
    """Why an override's reason is not one (TRUST-4: "x" sent a week past
    the gate), else None: at least MIN_REASON_CHARS characters and
    MIN_REASON_WORDS words of letters."""
    t = " ".join(str(text or "").split())
    if not t:
        return "Say why it can go out anyway."
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’-]*", t) if len(w) >= 2]
    if (len(t) < MIN_REASON_CHARS or len(words) < MIN_REASON_WORDS
            or len(set(t.replace(" ", "").casefold())) < 3):
        return "Say why in a few words — it is kept with this week."
    return None


def magnitude_of(b):
    """(size, unit) of a blocker's overage — the hourly budget's dollars
    ("$"), else its hours ("h") — or (None, None)."""
    over = b.get("over") if isinstance(b, dict) else None
    if isinstance(over, dict):
        for unit, k in (("$", "dollars"), ("h", "hours")):
            try:
                if over.get(k) is not None:
                    return float(over[k]), unit
            except (TypeError, ValueError):
                continue
    return None, None


def label(kind) -> str:
    k = str(kind or "")
    if k in LABELS:
        return LABELS[k]
    head, _sep, rest = k.partition(":")
    if head == "saved":
        return LABELS["saved"]
    words = rest.replace("_", " ") if rest else head
    return {"rule": f"Rule: {words}", "input": f"A setting the rules couldn't read ({words})",
            "stage": f"A repair step that didn't run ({words})", "roster": f"Roster: {words}",
            "soft": f"Held for automation: {words}"}.get(head, words.capitalize())


def record_shown(restaurant_id, history_id, blockers, db_path=None) -> int:
    """Keep each blocker (a {key, text}) as shown on this week — once per
    week and key. Never raises into the publish."""
    rows = [(restaurant_id, int(history_id), str(b.get("key"))[:300], kind_of(b.get("key")),
             str(b.get("text") or "")[:300], *magnitude_of(b)) for b in (blockers or []) if b and b.get("key")]
    if not rows or not history_id:
        return 0
    try:
        conn = get_conn(db_path)
        try:
            conn.executemany("INSERT OR IGNORE INTO schedule_publish_overrides (restaurant_id, history_id, key, kind, "
                             "text, magnitude, magnitude_unit) VALUES (?,?,?,?,?,?,?)", rows)
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
    blockers are recorded as shown too. A reason may be {reason, covered_by}
    — a manager gap's override names who runs the floor (ARCH-30), kept in
    `covered_by`. Never raises into the publish."""
    acked = {str(k) for k in (acked_keys or [])}
    if not acked or not history_id:
        return 0
    record_shown(restaurant_id, history_id, [b for b in blockers or [] if str(b.get("key")) in acked], db_path)
    reasons = reasons or {}
    who = ((actor or {}).get("username") or (actor or {}).get("email") or "") if isinstance(actor, dict) else ""
    sized = {str(b.get("key")): magnitude_of(b) for b in blockers or [] if b and b.get("key")}
    try:
        conn = get_conn(db_path)
        try:
            for k in acked:
                o = reasons.get(k)
                why = (o.get("reason") if isinstance(o, dict) else o) or ""
                cover = (o.get("covered_by") if isinstance(o, dict) else None) or ""
                # The size it went out at (TRUST-9): the band a later
                # demotion is held to.
                mag, unit = sized.get(k) or (None, None)
                conn.execute("UPDATE schedule_publish_overrides SET overridden_at=COALESCE(overridden_at, "
                             "datetime('now')), version=COALESCE(?, version), actor=?, authority=?, "
                             "reason=COALESCE(?, reason), covered_by=COALESCE(?, covered_by), "
                             "magnitude=COALESCE(?, magnitude), magnitude_unit=COALESCE(?, magnitude_unit) "
                             "WHERE restaurant_id=? AND history_id=? AND key=?",
                             (version, who[:120] or None, authority, str(why).strip()[:300] or None,
                              str(cover).strip()[:120] or None, mag, unit, restaurant_id, int(history_id),
                              k[:300]))
            conn.commit()
        finally:
            conn.close()
        return len(acked)
    except Exception as e:
        _capture(e, "record_overridden", restaurant_id)
        return 0


def stats(restaurant_id, days=WINDOW_DAYS, db_path=None) -> dict:
    """{kind: {kind, label, hard, shown, overridden, rate, confidence,
    last_overridden (M/D/YY), last_reason, demoted, accepted: {unit: the
    largest size sent past}}} over the last `days` — counted in WEEKS (a
    warning shown on one week is one, however many people or dates it
    named). Each row's kind is read from its KEY (kind_of), so a saved line
    kept under the old single "saved" bucket is learned in its own class
    (TRUST-9)."""
    import learning_stats
    from time_utils import mdy
    since = (datetime.utcnow() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT key, history_id, overridden_at, reason, magnitude, magnitude_unit "
                "FROM schedule_publish_overrides WHERE restaurant_id=? AND shown_at >= ?",
                (restaurant_id, since)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("publish overrides unreadable for %s: %s", restaurant_id, e)
        return {}
    weeks = {}
    for r in rows:
        w = weeks.setdefault((kind_of(r["key"]), r["history_id"]),
                             {"over": False, "last": None, "reason": None, "sizes": []})
        if r["overridden_at"]:
            w["over"] = True
            if w["last"] is None or str(r["overridden_at"]) > str(w["last"]):
                w["last"], w["reason"] = r["overridden_at"], r["reason"] or w["reason"]
            if r["magnitude"] is not None and r["magnitude_unit"]:
                w["sizes"].append((r["magnitude_unit"], float(r["magnitude"])))
    out = {}
    for (kind, _hid), w in sorted(weeks.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        e = out.setdefault(kind, {"kind": kind, "label": label(kind), "hard": hard(kind),
                                  "shown": 0, "overridden": 0, "last": None, "last_reason": None, "accepted": {}})
        e["shown"] += 1
        if w["over"]:
            e["overridden"] += 1
            if w["last"] and (e["last"] is None or str(w["last"]) > str(e["last"])):
                e["last"], e["last_reason"] = w["last"], w["reason"]
            for unit, size in w["sizes"]:
                e["accepted"][unit] = max(size, e["accepted"].get(unit, size))
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
        # `demotable` (schedule re-audit 10/11/26 TRUST-24, formerly `hard`):
        # whether the owner's record may quiet this kind — never its tier.
        # The tier (`tier`: must_fix / decide) is the colour both clients
        # draw; the old `hard` read as "red" on the phone and painted a
        # notice, a late change or an unscored week as a must-fix.
        b = dict(b, kind=kind, demotable=not hard(kind),
                 overridden_before=({"weeks": s["overridden"], "of": s["shown"], "last": s["last_overridden"]}
                                    if s and s["overridden"] else None))
        beyond = _beyond_accepted(b, s)
        if beyond:
            # Bigger than any the owner sent past: it holds the send
            # however often the smaller ones went out (TRUST-9).
            b["beyond_accepted"] = beyond
        if s and s.get("demoted") and not unattended and not beyond:
            text = (f"{b.get('text')} — you've sent past this in {s['overridden']} of {s['shown']} weeks, so it "
                    f"no longer holds the send")
            notes.append({"key": b.get("key"), "text": text, "demoted": True, "kind": kind})
            soft.append(text)
            continue
        blockers.append(b)
    # The must-fix tier first (TRUST-3), then the ones owners always send past
    # last, then the gate's own severity rank — never the date.
    blockers.sort(key=lambda b: ((b.get("tier") or tier_of_key(b.get("key"))) != "must_fix",
                                 ((st.get(b["kind"]) or {}).get("rate") or 0.0), b["demotable"], b.get("rank") or 0))
    always = sorted((dict(s) for s in st.values()
                     if s["overridden"] >= ALWAYS_MIN_WEEKS and (s["rate"] or 0) >= ALWAYS_MIN_RATE),
                    key=lambda s: (-(s["rate"] or 0), -s["overridden"], s["kind"]))
    return dict(review, blockers=blockers, notes=notes, soft=soft, always_overridden=always)


def _beyond_accepted(b, s):
    """A sized blocker (the hourly budget's overage) bigger than the
    largest the owner sent past, by more than DEMOTE_MAGNITUDE_SLACK —
    {size, unit, accepted} — else None. A blocker with no size, or a record
    with no sized overrides on it, is judged by frequency alone, as before;
    a sized one against a record of sizes in another unit is not demoted."""
    size, unit = magnitude_of(b)
    if size is None or not s or not s.get("overridden"):
        return None
    acc = (s.get("accepted") or {})
    if not acc:
        return None
    top = acc.get(unit)
    if top is None:
        return {"size": size, "unit": unit, "accepted": None}
    if size > float(top) * (1 + DEMOTE_MAGNITUDE_SLACK) + 1e-9:
        return {"size": size, "unit": unit, "accepted": float(top)}
    return None


# ── the owner's answers to the review's suggestions (TRUST-17) ─────────────

def dismiss(restaurant_id, key, scope="week", history_id=None, until=None, evidence=None, answer=None,
            actor=None, authority=None, db_path=None) -> bool:
    """Keep the owner's "not this week" / "keep it" answer to one review
    suggestion by key. `scope` "week" lasts until `until` (the week's last
    day, ISO); "every" until the suggestion's `evidence` signature changes
    or DISMISS_MAX_DAYS pass. One row per key: a newer answer replaces it."""
    key = str(key or "").strip()[:300]
    if not key:
        return False
    scope = scope if scope in DISMISS_SCOPES else "week"
    who = ((actor or {}).get("username") or (actor or {}).get("email") or "") if isinstance(actor, dict) else ""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM schedule_review_dismissals WHERE restaurant_id=? AND key=?", (restaurant_id, key))
            conn.execute("INSERT INTO schedule_review_dismissals (restaurant_id, key, scope, history_id, until_date, "
                         "evidence, answer, actor, authority) VALUES (?,?,?,?,?,?,?,?,?)",
                         (restaurant_id, key, scope, int(history_id) if history_id else None,
                          str(until)[:10] if until else None, str(evidence)[:200] if evidence else None,
                          str(answer or "")[:120] or None, who[:120] or None, authority))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        _capture(e, "dismiss", restaurant_id)
        return False


def undismiss(restaurant_id, key, db_path=None) -> bool:
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM schedule_review_dismissals WHERE restaurant_id=? AND key=?",
                         (restaurant_id, str(key or "")[:300]))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        _capture(e, "undismiss", restaurant_id)
        return False


def dismissals(restaurant_id, today=None, db_path=None) -> dict:
    """{key: {scope, until, evidence, answer, at}} — the answers still in
    force on `today`: a week's until its last day, a standing one for
    DISMISS_MAX_DAYS. The caller compares `evidence` with the suggestion's
    own now (dismissed_now)."""
    t = today or date.today()
    if isinstance(t, datetime):
        t = t.date()
    since = (datetime.utcnow() - timedelta(days=DISMISS_MAX_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT key, scope, until_date, evidence, answer, dismissed_at FROM "
                                "schedule_review_dismissals WHERE restaurant_id=? AND dismissed_at >= ?",
                                (restaurant_id, since)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("review dismissals unreadable for %s: %s", restaurant_id, e)
        return {}
    out = {}
    for r in rows:
        if r["scope"] == "week" and r["until_date"] and str(r["until_date"])[:10] < t.isoformat():
            continue
        out[r["key"]] = {"scope": r["scope"], "until": r["until_date"], "evidence": r["evidence"],
                         "answer": r["answer"], "at": r["dismissed_at"]}
    return out


def dismissed_now(held, key, evidence=None) -> bool:
    """Whether a suggestion keyed `key` with this `evidence` signature is
    answered: a week's answer for its key, a standing one only while its
    evidence is what the owner saw (a floor kept on 3 weeks of data comes
    back when the data says something new)."""
    d = (held or {}).get(key)
    if not d:
        return False
    if d.get("scope") == "every" and (d.get("evidence") or None) != (str(evidence)[:200] if evidence else None):
        return False
    return True


def _capture(e, what, restaurant_id):
    log.warning("schedule_overrides %s failed for %s: %s", what, restaurant_id, e)
    try:
        import ops
        ops.capture(e, job="schedule_overrides", context=f"{what} restaurant_id={restaurant_id}",
                    restaurant_id=restaurant_id)
    except Exception:
        pass


def reasons_from(payload) -> dict:
    """{key: reason} from a request's `overrides: [{key, reason, covered_by?,
    legal_ack?}]` (the pre-publish screen's contract, prepublish). An
    override naming who runs the floor through a manager gap (ARCH-30) is
    {key: {reason, covered_by}}; one that acknowledges a law it goes out
    past (TRUST-4, TRUST-8 — the notice a jurisdiction requires) carries
    `legal_ack: True`."""
    out = {}
    for o in (payload or []) if isinstance(payload, (list, tuple)) else []:
        if isinstance(o, dict) and o.get("key"):
            reason = str(o.get("reason") or "").strip()[:300]
            cover = " ".join(str(o.get("covered_by") or "").split())[:120]
            legal = o.get("legal_ack") is True
            if cover or legal:
                d = {"reason": reason, "covered_by": cover}
                if legal:
                    d["legal_ack"] = True
                out[str(o["key"])[:300]] = d
            else:
                out[str(o["key"])[:300]] = reason
    return out
