"""
prepublish.py — the pre-publish review: ONE payload the web Studio and the
phone both render before a week goes to staff (schedule audit 10/10/26 RM-6,
contract #6; API_REFERENCE.md "The pre-publish review").

Before this, the answers an owner needs before pressing Send were spread
across the Summary, the Fix and Shift tabs, Crew, Intel and History, some
only on the web and some only in a payload the phone could not decode; the
Summary's tiles read the client's own rules recheck while the list under
them read the server's gate, so "Every rule kept" sat above "Read before
this goes out · 2" (TRUST-4). One payload, computed against the rows as they
stand NOW (or the rows on screen, posted), answers the seven questions:

  1. is anything illegal or unsafe?          must_fix (TRUST-2, 3, ARCH-30)
  2. what do I have to decide?               decide   (scoped one-tap answers)
  3. what will it cost, how far to trust it? numbers  (ranges + record, TRUST-7, 19, 30, 32)
  4. what changed?                           changed  (TRUST-24)
  5. where is it fragile?                    fragile  (TRUST-26)
  6. why these people, why not the others?   why      (TRUST-13, 14)
  7. has this been right before?             sure     (TRUST-18, 20)

plus the one verdict (TRUST-4, 6), the quality score graded against the
restaurant's own weeks (TRUST-29) and what Send would do (TRUST-33: the
publish gate runs once here — `schedule_publish.publish_review` — and every
tier, count and the one-tap verdict are read from that one run).

L2: no Flask. strategy_routes serves it on both prefixes. Every section is
computed on its own and a section that cannot be worked out is empty or
null — never a made-up figure, never a failed page.

Schedule re-audit 10/11/26 (workstream T) — additive to the one payload:
  * `why` — each explained shift is the ENGINE's own reason when the solve
    that drafted it recorded one (review_json.assignment_reasons, persisted
    by schedule_job from the final solve — contract #4; `basis: "engine"`),
    else a comparison made after the draft, labelled so (`basis: "after"`);
    a comparison never argues against the pick — a stronger runner-up is
    said as one, with a one-tap swap (TRUST-1, TRUST-2). `why.engine` says
    whether reasons were recorded; `why.draft` which engine drafted and what
    the critic kept or refused. The shifts explained are chosen by risk —
    the sales-busiest nights' closers, keyholders, new people, the rows the
    owner usually changes — one per person (TRUST-12).
  * `verdict` never says "nothing breaks your rules" over a rule-labelled
    breach or overtime; `worth_a_look` (TRUST-3).
  * `must_fix_groups` / `decide_groups` — one card per rule kind with one
    fix for the group (TRUST-15); `suggestions` — the decide items that do
    not hold the send, counted apart (TRUST-16).
  * each must-fix item's `cls` / `law` / `override_allowed` /
    `override.needs` ("reason" | "covered_by" | "legal_ack") / `override.legal`
    / `override.min_reason` — the law is fixed or not sent; a manager gap
    names a real manager or owner (TRUST-4, TRUST-5, TRUST-8).
  * `always_overridden` — the warnings this owner always sends past, each
    with a one-tap change to the rule (TRUST-10).
  * `sure.confidence` — the K1 confidence engine over how the published
    weeks RAN (schedule_learning_scores), never over owner edits (TRUST-6).
  * dismissals are kept server-side (schedule_overrides.dismiss, TRUST-17);
    `sections="core"` computes what Send needs first and leaves the
    explanations for a second call (`explain_pending`), and a review of
    the same rows, rules and day is served from a short cache (TRUST-26).
"""
import json
import logging
import os
import threading
import time as _time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

KEPT_WEEKS = 8                 # the acceptance window "your last N drafts"
QUALITY_WEEKS = 12             # the weeks a quality score is graded against
QUALITY_MIN_WEEKS = 5          # fewer and the score is "ungraded"
QUALITY_GOOD_PCTILE = 60       # at or above: better than most of your weeks
QUALITY_WEAK_PCTILE = 25       # below: weaker than most of your weeks
FORECAST_MIN_WEEKS = 4         # scored weekly projections before a range is shown
FORECAST_BAND = 0.8            # the share of past misses the range covers
FIX_LOOKUPS = 8                # must-fix items that get a computed fix (each is a legality search)
REPLACEMENT_CHECKS = 15        # candidates tried per item, cheapest first
# The legality searches of one review stop after this many seconds (TRUST-26:
# 375 rows took 4.5s, most of it here); an item past it gets its group's fix.
FIX_SECONDS = 1.5
CREW_DECIDE = 3                # Crew shifts leaner than usual offered as decisions
REGULAR_HOURS = 16             # "a regular": usually at least this many hours a week
# "Why these people" (TRUST-12): how many shifts are explained, one per person.
WHY_LIMIT = 5
# "How sure" (TRUST-6): a published week RAN as planned when every measured
# test passed — no minute without a manager, no uncovered no-show, at most
# RAN_SHORTFALL_HOURS short of the asked staffing, labor at most
# RAN_LABOR_OVER_PTS over its target, overtime at most RAN_OT_OVER_HOURS
# past what the week planned. Below SURE_MIN_WEEKS measured weeks the
# figure is "—" (the K1 engine's own track-record floor applies on top).
RAN_SHORTFALL_HOURS = 2.0
RAN_LABOR_OVER_PTS = 3.0
RAN_OT_OVER_HOURS = 2.0
SURE_MIN_WEEKS = 2
SURE_WEEKS = 12
# A review of the same rows, rules and day is served from here (TRUST-26).
# The key holds everything a review reads (_cache_key: the rows or the
# stored version, the week's state, schedule_inputs_version, the dismissals
# and overrides, the reader, the day), so the time is only a backstop: 30 s
# had a Studio session rebuild a 10-40 s review between the Summary, the
# Publish step and the phone (scale audit 10/19/26 A5-F5 / A6 FE-11).
CACHE_SECONDS = float(os.getenv("PREPUBLISH_CACHE_SECONDS", "300"))
CACHE_MAX = 256
# Rule kinds counted per person-week, not per shift (TRUST-11): a weekly
# ceiling, a minimum, days in a row.
PERSON_WEEK_KINDS = ("over_hours_ceiling", "under_min_hours", "over_max_hours", "long_run", "minor_week_hours",
                     "days_off", "payroll_tail_full")
# The one fix offered for a whole group of one rule's breaches (TRUST-15).
GROUP_FIX_LABELS = {"long_run": "Give each a day off (shows each change)",
                    "rest_gap": "Space out the short turnarounds (shows each change)",
                    "shift_too_long": "Shorten the long shifts (shows each change)",
                    "no_manager": "Cover each stretch with a manager (shows each change)",
                    "coverage_floor": "Fill each short shift (shows each change)"}
# Soft kinds that are reminders, not decisions: one FYI line each (TRUST-22).
REMINDER_KINDS = ("meal_break", "shift_too_short")
# The edit and endpoint names a `post` action may name (API_REFERENCE).
POST_PATHS = ("labor/crew-matrix", "labor/standby/ask", "labor/rules")


def _capture(e, what, restaurant_id):
    log.warning("prepublish %s failed for %s: %r", what, restaurant_id, e)
    try:
        import ops
        ops.capture(e, job="prepublish", context=f"{what} restaurant_id={restaurant_id}", restaurant_id=restaurant_id)
    except Exception:
        pass


def _mdy(value) -> str:
    from time_utils import mdy
    return mdy(value) or ""


def _low(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _weekday(iso) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%A")
    except (TypeError, ValueError):
        return ""


def _part(row) -> str:
    """"Lunch" / "Dinner" for a row (schedule_time — TRUST-31)."""
    import schedule_time as _st
    try:
        return _st.part_word(_st.primary_daypart(row))
    except Exception:
        return ""


def _hours(row) -> float:
    import schedule_rules as _sr
    try:
        return float(_sr.row_hours(row))
    except Exception:
        return 0.0


def _match(row) -> dict:
    """How a client finds one row of the week on screen: its id when it has
    one, else the person, date and start."""
    return {"shift_id": row.get("shift_id") or None, "employee": row.get("employee") or "",
            "date": row.get("date") or "", "shift_start": row.get("shift_start") or ""}


def _row_out(row) -> dict:
    keep = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")
    return {k: row.get(k) or "" for k in keep}


def _week_dates(row) -> list:
    try:
        ws = date.fromisoformat(str(row["week_start"])[:10])
        we = date.fromisoformat(str(row["week_end"] or row["week_start"])[:10])
    except (TypeError, ValueError, KeyError):
        return []
    return [(ws + timedelta(days=i)).isoformat() for i in range(max(0, min(13, (we - ws).days)) + 1)]


def _plural(n, one, many=None) -> str:
    return one if n == 1 else (many or one + "s")


# ── the one entry point ────────────────────────────────────────────────────

SECTIONS = ("all", "core", "explain")
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def review(restaurant_id, schedule_id, rows=None, user=None, today=None, can_publish=True, sections="all") -> dict:
    """The contract #6 payload for `schedule_id` (see the module doc and
    API_REFERENCE). `rows` — the week as it stands on screen (unsaved
    edits), checked without saving; else the stored working copy. `user` —
    the reader (their labor basis, the override candidates). `sections`
    "core" computes what Send needs (the gate's items, the verdict, the
    numbers, the send) and leaves the explanations empty with
    `explain_pending` — the web's re-check after each edit; "all" (the
    default, the phone) everything. Served from a CACHE_SECONDS cache for
    the same rows, rules, readers and day (TRUST-26). Never raises: a
    section that could not be worked out is empty."""
    sections = sections if sections in SECTIONS else "all"
    # The saved week's review is cached (the Summary, the Publish step and
    # the phone ask for it in turn) — and the rows on screen too, by their
    # own text (scale audit 10/19/26 A5-F5: the same unsaved rows checked by
    # the Summary and again by the Publish step paid twice); an edit is new
    # rows, so it is computed — "core" first (TRUST-26).
    ck = _safe(lambda: _cache_key(restaurant_id, schedule_id, rows, user, today, can_publish, sections), None,
               "cache key", restaurant_id)
    hit = _cache_get(ck)
    if hit is not None:
        return hit
    out = _review(restaurant_id, schedule_id, rows=rows, user=user, today=today, can_publish=can_publish,
                  sections=sections)
    if out.get("ok"):
        _cache_put(ck, out)
    return out


def _review(restaurant_id, schedule_id, rows=None, user=None, today=None, can_publish=True, sections="all"):
    import schedule_publish as _sp
    import schedule_overrides as _so
    rid = restaurant_id
    keep = {}
    gate = None
    try:
        gate = _sp.publish_review(rid, schedule_id, rows=rows, keep=keep, today=today)
    except Exception as e:
        _capture(e, "gate", rid)
    if not keep.get("row"):
        row = _row(rid, schedule_id)
        if row is None:
            return {"ok": False, "error": "Not found"}
        keep["row"] = row
        keep.setdefault("rows", [dict(r) for r in rows] if rows is not None else _stored_rows(row))
    if gate is None:
        gate = {"blockers": [{"key": "check_failed", "tier": _sp.MUST_FIX, "rank": -1, "date": None, "cls": "check",
                              "law": False,
                              "text": "The publish check couldn't run just now — try again before sending"}],
                "soft": [], "notes": [], "held_unattended": [], "hours": None}
    row, week_rows = keep["row"], keep.get("rows") or []
    # Every blocker this review shows is "shown" on the override record
    # (BUGSLIFE-12): a week the owner fixes counts against how often they
    # send past it, so the list no longer learns to stop asking from the
    # weeks they sent past alone.
    if not row.get("superseded_by"):
        _safe(lambda: _so.record_shown(rid, row["id"], [b for b in gate.get("blockers") or []
                                                        if b.get("key") != "check_failed"]),
              0, "record shown", rid)
    c, viols = keep.get("constraints"), keep.get("violations") or []
    saved_review, quality = keep.get("review") or {}, keep.get("quality") or {}
    dates = _week_dates(row)
    held = _safe(lambda: _so.dismissals(rid, today=today), {}, "dismissals", rid)
    memo = {"held": held, "user": user, "deadline": _time.monotonic() + FIX_SECONDS}
    scores = _safe(lambda: __import__("models").get_operational_scores(rid), {}, "scores", rid)
    out = {"ok": True, "schedule_id": row["id"], "week_start": row["week_start"], "week_end": row["week_end"],
           "published_at": row.get("published_at"), "version": _version(rid, row["id"]),
           "checked_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
           "rows_source": "screen" if rows is not None else "saved", "can_publish": bool(can_publish),
           "sections": sections, "explain_pending": sections == "core",
           "replaced_reason": _safe(lambda: __import__("schedule_versions").replaced_error(row), None, "replaced", rid)}
    core = sections in ("all", "core")
    explain = sections in ("all", "explain")
    crew = _safe(lambda: _crew(rid, row, rows), None, "crew", rid)
    left_off = _safe(lambda: left_off_reasons(rid, week_rows, c, scores, dates, viols=viols, crew=crew), [],
                     "left_off", rid)
    if core:
        out["must_fix"] = _safe(lambda: _must_fix(rid, gate, keep, user, scores, memo), [], "must_fix", rid)
        out["decide"] = _safe(lambda: _decide(rid, gate, keep, crew, left_off, memo), [], "decide", rid)
        out["fyi"] = _safe(lambda: _fyi(gate, viols), [], "fyi", rid)
        out["numbers"] = _safe(lambda: _numbers(rid, row, week_rows, c, dates, quality), _empty_numbers(), "numbers", rid)
        out["quality"] = _safe(lambda: _quality(rid, row, quality), _empty_quality(), "quality", rid)
        out["send"] = _safe(lambda: _send(rid, row, week_rows, gate, out, can_publish), _empty_send(), "send", rid)
        out["must_fix_groups"] = _safe(lambda: group_items(out["must_fix"], "must_fix"), [], "groups", rid)
        out["decide_groups"] = _safe(lambda: group_items([d for d in out["decide"] if d.get("holds")], "decide"),
                                     [], "groups", rid)
        out["suggestions"] = [d for d in out["decide"] if not d.get("holds")]
        out["always_overridden"] = _safe(lambda: _always_overridden(rid, gate, c), [], "always overridden", rid)
        # The week's hours split by pay, as the gate read them (E-7): the
        # phone's "the week's hours" line reads it here, so the Send sheet
        # runs the gate once (re-audit 10/11/26 TRUST-25), never the publish
        # check beside the review.
        out["hours"] = gate.get("hours")
    else:
        out.update(must_fix=[], decide=[], fyi=[], numbers=_empty_numbers(), quality=_empty_quality(),
                   send=_empty_send(), must_fix_groups=[], decide_groups=[], suggestions=[], always_overridden=[],
                   hours=None)
    out["changed"] = {"vs_last_week": [], "vs_usual": [], "vs_last_draft": [], "cavnar_changes": []}
    out["narrative"], out["fragile"] = [], []
    out["why"] = {"assignments": [], "left_off": left_off, "engine": None, "draft": None}
    out["likely_to_change"] = None
    out["sure"] = _empty_sure()
    if explain:
        out["changed"] = _safe(lambda: _changed(rid, row, week_rows, crew, saved_review, rows is not None),
                               out["changed"], "changed", rid)
        out["narrative"] = _safe(lambda: _narrative(saved_review), [], "narrative", rid)
        out["fragile"] = _safe(lambda: _fragile(rid, week_rows, c, dates, quality), [], "fragile", rid)
        likely = None
        if not row.get("published_at"):
            likely = _safe(lambda: __import__("schedule_learning").likely_to_change(rid, week_rows), None, "likely",
                           rid)
        out["likely_to_change"] = likely
        out["why"]["engine"] = _safe(lambda: engine_status(saved_review, week_rows), None, "engine", rid)
        out["why"]["draft"] = _safe(lambda: draft_account(saved_review), None, "draft", rid)
        out["why"]["assignments"] = _safe(
            lambda: weighed_assignments(rid, week_rows, c, scores, quality, saved_review=saved_review, likely=likely,
                                        econ=_econ_of(row)), [], "why", rid)
        out["sure"] = _safe(lambda: _sure(rid, quality, likely, out.get("numbers")), _empty_sure(), "sure", rid)
    must_n = len(out["must_fix"])
    holding = [d for d in out["decide"] if d.get("holds")]
    out["counts"] = {"must_fix": must_n, "decide": len(out["decide"]), "decide_holding": len(holding),
                     "suggestions": len(out["decide"]) - len(holding),
                     "must_fix_groups": len(out["must_fix_groups"]), "decide_groups": len(out["decide_groups"]),
                     # What the Summary's "To fix or decide" and the Publish
                     # header both count (TRUST-16): what holds the send.
                     "to_fix_or_decide": must_n + len(holding),
                     "fyi": len(out["fyi"]), "warnings": must_n + len(out["decide"])}
    out["verdict"] = verdict(out["must_fix"], out["decide"], row, week_rows, rows is not None, fyi=out["fyi"],
                             numbers=out["numbers"], groups=out["must_fix_groups"],
                             decide_groups=out["decide_groups"]) if core else \
        {"tone": None, "sentence": "", "subline": ""}
    out["gate"] = {"blocker_keys": [b["key"] for b in gate.get("blockers") or []],
                   "always_overridden": gate.get("always_overridden") or []}
    out["drift"] = _safe(lambda: drift(row, gate, saved_review, today), None, "drift", rid) if core else None
    return out


# ── what changed since the draft was built ─────────────────────────────────

# Rule kinds a draft is never built with once its inputs say so, so more of
# them now than when it was built is the world moving under it: someone off
# the roster, time off approved, availability changed, shifts worked since
# that now reach into the week. Each with the words the banner says.
DRIFT_KINDS = {
    "inactive": "left the roster", "off_roster": "left the roster",
    "approved_time_off": "had time off approved", "pending_time_off": "asked for time off",
    "unavailable_day": "changed their availability", "unavailable_daypart": "changed their availability",
    "outside_window": "changed their availability", "note_unavailable": "changed their availability",
    "long_run": "worked shifts since that run into this week", "rest_gap": "worked shifts since that run into this week",
}


def drift(row, gate, saved_review, today=None):
    """{built_mdy, days_ago, reasons, dates, count} when this draft's own
    problems grew since it was built because its inputs moved — the roster,
    time off, availability, shifts worked since — else None (owner,
    10/14/26: a four-day-old draft showed six "must fix" of which five came
    from after it was built). `dates` are the days to rebuild, never a day
    already worked."""
    import schedule_overrides as _so
    built = str(row.get("generated_at") or "")[:19]
    if not built or row.get("published_at"):
        return None
    then = (saved_review or {}).get("by_kind") or {}
    now_kinds, who, dates = {}, {}, set()
    for b in gate.get("blockers") or []:
        kind = str(_so.kind_of(b.get("key") or "") or "").split(":", 1)[-1]
        if kind not in DRIFT_KINDS:
            continue
        now_kinds[kind] = now_kinds.get(kind, 0) + 1
        name = str(b.get("employee") or str(b.get("text") or "").split(" — ")[0]).strip()
        who.setdefault(DRIFT_KINDS[kind], [])
        if name and name not in who[DRIFT_KINDS[kind]]:
            who[DRIFT_KINDS[kind]].append(name)
        if b.get("date"):
            dates.add(str(b["date"])[:10])
    grown = {k for k, n in now_kinds.items() if n > int(then.get(k) or 0)}
    if not grown:
        return None
    reasons, done = [], set()
    for k in sorted(grown, key=lambda x: list(DRIFT_KINDS).index(x)):
        words = DRIFT_KINDS[k]
        if words in done:
            continue
        done.add(words)
        names = who.get(words) or []
        lead = (", ".join(names[:-1]) + " and " + names[-1]) if len(names) > 1 else (names[0] if names else "Someone")
        if len(names) > 3:
            lead = ", ".join(names[:3]) + f" and {len(names) - 3} more"
        reasons.append(f"{lead} {words}")
    t = today or date.today()
    t_iso = t.isoformat() if hasattr(t, "isoformat") else str(t)[:10]
    try:
        days_ago = (date.fromisoformat(t_iso) - date.fromisoformat(built[:10])).days
    except ValueError:
        days_ago = None
    return {"built_at": built, "built_mdy": _mdy(built[:10]), "days_ago": days_ago, "reasons": reasons,
            "dates": sorted(d for d in dates if d >= t_iso), "count": sum(now_kinds[k] for k in grown)}


# ── the cache (TRUST-26) ───────────────────────────────────────────────────

def _cache_key(rid, schedule_id, rows, user, today, can_publish, sections):
    """What a review depends on: the rows (on screen, or the stored
    version), the week's state, the restaurant's settings, the roster, time
    off and availability, the reader, the day. None when it cannot be told
    (never cached then)."""
    import hashlib
    import models as _m
    from models import get_conn
    conn = get_conn()
    try:
        h = conn.execute("SELECT published_at, superseded_by, edited_at FROM schedule_history WHERE id=? AND "
                         "restaurant_id=?", (int(schedule_id), rid)).fetchone()
        if h is None:
            return None
        stamps = []
        for sql in ("SELECT COUNT(*) AS n, MAX(dismissed_at) AS a FROM schedule_review_dismissals "
                    "WHERE restaurant_id=?",
                    "SELECT COUNT(*) AS n, MAX(overridden_at) AS a FROM schedule_publish_overrides "
                    "WHERE restaurant_id=?"):
            r = conn.execute(sql, (rid,)).fetchone()
            stamps.append((r["n"], r["a"]))
    finally:
        conn.close()
    # The schedule's inputs as one stamp (models.schedule_inputs_version —
    # the restaurant row, shift history, people, availability, time off,
    # roles, ratings …): the Studio's kept inputs are keyed on it too.
    rsig = _m.schedule_inputs_version(rid)
    rows_sig = json.dumps(rows, sort_keys=True, default=str) if rows is not None else ("saved", _version(rid, schedule_id))
    who = ""
    if isinstance(user, dict):
        who = "|".join(str(user.get(k) or "") for k in ("id", "username", "role", "is_admin", "is_principal"))
    raw = json.dumps([rid, int(schedule_id), dict(h), stamps, rsig, rows_sig, who, str(today), bool(can_publish),
                      sections], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_get(key):
    if key is None:
        return None
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is None or _time.monotonic() - hit[0] > CACHE_SECONDS:
            _CACHE.pop(key, None)
            return None
    return json.loads(hit[1])


def _cache_put(key, out):
    if key is None:
        return
    try:
        blob = json.dumps(out, default=str)
    except (TypeError, ValueError):
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_MAX:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:max(1, CACHE_MAX // 4)]:
                _CACHE.pop(k, None)
        _CACHE[key] = (_time.monotonic(), blob)


def invalidate(restaurant_id=None):
    """Drop the cached reviews (all of them: a key does not say whose it
    is). Called when the owner answers a suggestion."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _safe(fn, default, what, rid):
    try:
        v = fn()
        return default if v is None and default is not None else v
    except Exception as e:
        _capture(e, what, rid)
        return default


def _row(rid, schedule_id):
    from models import get_conn
    conn = get_conn()
    try:
        import models as _m_pp
        _m_pp._ensure_history_columns(conn)
        r = conn.execute("SELECT id, week_start, week_end, schedule_csv, review_json, review_ref, quality_json, "
                         "quality_ref, economics_json, published_at, superseded_by FROM schedule_history "
                         "WHERE id=? AND restaurant_id=?", (int(schedule_id), rid)).fetchone()
        if r is None:
            return None
        d = dict(r)
        # The whole review and evaluation, whatever is kept inline (SCALE-13):
        # every reader here parses these two as before.
        d["review_json"] = json.dumps(_m_pp.history_review(conn, d), default=str)
        d["quality_json"] = json.dumps(_m_pp.history_quality(conn, d), default=str)
    finally:
        conn.close()
    return d


def _stored_rows(row) -> list:
    """The week's WORKING rows, from the shift table (schedule_shifts —
    schedule re-audit 10/11/26 ARCH-7)."""
    import schedule_shifts
    return schedule_shifts.rows_of(row.get("id"), "working") if row.get("id") else []


def _econ_of(row) -> dict:
    try:
        return json.loads(row.get("economics_json") or "null") or {}
    except Exception:
        return {}


def _version(rid, hid):
    try:
        import schedule_versions as _sv
        v = _sv.newest_version(rid, hid)
        return v[0].get("version") if v else None
    except Exception:
        return None


# ── 0. the verdict ─────────────────────────────────────────────────────────

def worth_a_look(fyi, numbers) -> list:
    """What does not hold the send but is a rule-labelled breach or a cost
    the owner should see before it goes (TRUST-3): the soft rule groups of
    the FYI (never the gate's own notes), overtime first, said with its
    figure. [{key, text}]."""
    out = []
    ot = (numbers or {}).get("overtime") or {}
    soft = [f for f in fyi or [] if f.get("kind") not in (None, "note") and f.get("kind") not in REMINDER_KINDS
            and str(f.get("key") or "").startswith("soft:")]
    if ot.get("priced") and ot.get("hours"):
        prem = ot.get("premium")
        out.append({"key": "overtime", "text": f"{ot['hours']:g}h of overtime"
                                               + (f" (${prem:,.0f} premium)" if prem else "")})
        soft = [f for f in soft if f.get("kind") not in ("over_hours_ceiling", "daily_ot")]
    for f in soft:
        import schedule_rules as _sr
        lab = _sr.LABELS.get(f.get("kind")) or str(f.get("kind") or "").replace("_", " ")
        n = int(f.get("count") or 1)
        noun = "person" if f.get("kind") in PERSON_WEEK_KINDS else "shift"
        out.append({"key": f.get("key"), "text": f"{n} {_plural(n, noun, 'people' if noun == 'person' else None)} "
                                                 f"{lab}"})
    return out


def verdict(must_fix, decide, row, rows, screen=False, fyi=None, numbers=None, groups=None,
            decide_groups=None) -> dict:
    """The one verdict both clients lead with (TRUST-4, TRUST-6): red when
    anything must be fixed, amber when decisions are owed, green only when
    neither — never "The week is built ✓" over a broken week.

    It says what is TRUE (re-audit 10/11/26 TRUST-3): "Nothing breaks your
    rules" only when nothing does — a soft rule breach or overtime is named
    on the verdict ("Ready to send — 2 things worth a look: 25.5h of
    overtime …"), never left to a folded list under a green line. A wall of
    one rule's breaches is said as its groups (TRUST-15)."""
    holding = [d for d in decide if d.get("holds")]
    look = worth_a_look(fyi, numbers)
    look_txt = "; ".join(x["text"] for x in look[:2]) + ("…" if len(look) > 2 else "")
    groups = groups or []
    if must_fix:
        if len(must_fix) == 1 or len(groups) <= 1 and len(must_fix) <= 2:
            first = must_fix[0]["text"].rstrip(".")
            more = len(must_fix) - 1
            sentence = f"Not ready: {first}" + (f", and {more} more to fix" if more else "") + "."
        else:
            heads = "; ".join(g["text"].rstrip(".") for g in groups[:2]) + ("…" if len(groups) > 2 else "")
            sentence = f"Not ready: {len(must_fix)} things to fix — {heads}."
        tone = "red"
    elif holding:
        n = len(decide_groups) if decide_groups else len(holding)
        head = ""
        if decide_groups and len(decide_groups) == 1 and decide_groups[0].get("count", 1) > 1:
            head = f": {decide_groups[0]['text'].rstrip('.')}"
        tone = "amber"
        sentence = (f"Ready after {n} {_plural(n, 'decision')}{head}." +
                    (f" Also worth a look: {look_txt}." if look else " Everything else checks out."))
    elif look:
        tone = "green"
        sentence = (f"Ready to send — nothing holds it, but {len(look)} {_plural(len(look), 'thing')} "
                    f"worth a look: {look_txt}.")
    else:
        tone, sentence = "green", "Ready to send. Nothing breaks your rules, and nothing needs a decision."
    people = {_low(r.get("employee")) for r in rows if (r.get("employee") or "").strip()}
    hrs = sum(_hours(r) for r in rows)
    sub = (f"Week of {_mdy(row['week_start'])} – {_mdy(row['week_end'] or row['week_start'])} · {len(rows)} "
           f"{_plural(len(rows), 'shift')} · {len(people)} {_plural(len(people), 'person', 'people')} · "
           f"{round(hrs):,}h · checked against today's time off and availability"
           + (" · your unsaved edits included" if screen else ""))
    return {"tone": tone, "sentence": sentence, "subline": sub, "worth_a_look": look}


# ── 1. must fix ────────────────────────────────────────────────────────────

def _must_fix(rid, gate, keep, user, scores, memo=None) -> list:
    import schedule_publish as _sp
    import schedule_overrides as _so
    rows, c = keep.get("rows") or [], keep.get("constraints")
    breaches = keep.get("breaches") or {}
    memo = memo if memo is not None else {"deadline": _time.monotonic() + FIX_SECONDS}
    prepared = None
    offered = {}         # who a one-tap fix already names, and how many hours (TRUST-14)
    out = []
    for n, b in enumerate(b for b in gate.get("blockers") or [] if _sp.blocker_tier(b) == _sp.MUST_FIX):
        key, kind = b["key"], _so.kind_of(b["key"])
        v = breaches.get(key)
        pol = _sp.override_policy(b)
        needs = pol["needs"]
        item = {"key": key, "kind": kind, "tier": "must_fix", "rank": b.get("rank"), "text": b["text"],
                "date": b.get("date"), "day": _weekday(b.get("date")) if b.get("date") else None,
                # Where the rule comes from, and how it may be sent past
                # (TRUST-4, TRUST-5, TRUST-8): the law is fixed or not sent.
                "cls": b.get("cls") or "check", "law": bool(b.get("law")),
                "fixes": [], "override_allowed": bool(pol["allowed"]),
                "override": {"needs": needs, "candidates": [], "why": pol.get("why"),
                             "min_reason": {"chars": _so.MIN_REASON_CHARS, "words": _so.MIN_REASON_WORDS},
                             "legal": b.get("legal"),
                             "prompt": ("Which manager or owner runs the floor through it? You count."
                                        if needs == "covered_by" else
                                        ((b.get("legal") or {}).get("text") if needs == "legal_ack" else
                                         ("Why is it fine to send it anyway?" if needs else None)))},
                "overridden_before": b.get("overridden_before")}
        if needs == "covered_by":
            item["override"]["candidates"] = _floor_candidates(rows, c, v, user, b)
        if n < FIX_LOOKUPS and c is not None and _time.monotonic() < memo["deadline"]:
            try:
                if kind == "rule:no_manager" and v:
                    item["fixes"] = _manager_fixes(rows, c, v)
                elif kind.startswith("rule:") and v and not v.get("day_level") and v.get("index") is not None:
                    if prepared is None:
                        import schedule_engine as _se
                        prepared = _se.prepare_replacements(rid, rows, constraints=c)
                    item["fixes"] = _replacement_fixes(rid, rows, c, int(v["index"]), prepared, scores,
                                                       memo=memo, offered=offered)
                elif kind in ("rule:coverage_floor", "rule:nobody_at_close", "rule:keyholder_until_close") and v:
                    item["fixes"] = _floor_fixes(rid, rows, c, v)
            except Exception as e:
                _capture(e, f"fix {kind}", rid)
        if kind == "roster:no_manager_roster":
            item["fixes"].append({"label": "Name your managers", "action": "open",
                                  "payload": {"stage": "setup", "panel": "managers"}})
        elif needs == "covered_by":
            # Somebody not listed runs the floor: they are put on the week
            # as a manager's shift, or named a manager in Team — never typed
            # as a free name (TRUST-5).
            item["fixes"].append({"label": "Someone else? Add them as a manager in Team", "action": "open",
                                  "payload": {"stage": "team", "panel": "managers"}})
        elif kind == "unwritten":
            ds = [d for d in key.split(":", 1)[1].split(",") if d]
            item["fixes"].append({"label": "Redo " + ("that day" if len(ds) == 1 else "those days"), "action": "open",
                                  "payload": {"stage": "schedule", "redo": ds}})
        elif kind.startswith("input:"):
            who = key.split(":", 2)[2] if key.count(":") >= 2 else ""
            item["fixes"].append({"label": "Fix it in Team", "action": "open",
                                  "payload": {"stage": "team", "employee": who}})
        if kind.startswith("rule:") or kind in ("needs_review", "stage"):
            item["fixes"].append({"label": "Fix every hard rule (shows each change)", "action": "apply_fixes",
                                  "payload": {}})
        if v and v.get("index") is not None and not v.get("day_level") and 0 <= int(v["index"]) < len(rows):
            r = rows[int(v["index"])]
            item["fixes"].append({"label": "Show the shift", "action": "open",
                                  "payload": {"stage": "schedule", "date": r.get("date"), "shift_id": r.get("shift_id"),
                                              "employee": r.get("employee")}})
        out.append(item)
    return out


def _manager_fixes(rows, c, v) -> list:
    """The manager backstop's own legal answer for that day's stretch
    (schedule_rules.cover_manager_gaps on that date alone): a manager run on
    or brought in, as an edit to the rows on screen."""
    import schedule_rules as _sr
    d = v.get("date")
    if not d:
        return []
    got = _sr.cover_manager_gaps([dict(r) for r in rows], c, editable={d})
    fixes = []
    new_rows = got.get("rows") or []
    for e in (got.get("extended") or [])[:2]:
        i = e.get("index")
        if i is None or i >= len(rows) or i >= len(new_rows):
            continue
        was, now = rows[i], new_rows[i]
        if (was.get("shift_start") or "") == (now.get("shift_start") or ""):
            lab = f"Extend {e['employee']} to {now.get('shift_end')}"
        elif (was.get("shift_end") or "") == (now.get("shift_end") or ""):
            lab = f"Bring {e['employee']} in at {now.get('shift_start')}"
        else:
            lab = f"Move {e['employee']} to {now.get('shift_start')}–{now.get('shift_end')}"
        fixes.append({"label": lab,
                      "reason": e.get("reason"), "action": "edit_rows",
                      "payload": {"change": [{"match": _match(was),
                                              "set": {"shift_start": now.get("shift_start"),
                                                      "shift_end": now.get("shift_end")}}]}})
    for a in (got.get("added") or [])[:2]:
        i = a.get("index")
        if i is None or i >= len(new_rows):
            continue
        nr = _row_out(new_rows[i])
        fixes.append({"label": f"Add {a['employee']} {nr['shift_start']}–{nr['shift_end']}", "reason": a.get("reason"),
                      "action": "edit_rows", "payload": {"add": [nr]}})
    for left in (got.get("left") or [])[:1]:
        why = "; ".join(f"{x['employee']}: {x['why']}" for x in (left.get("reasons") or [])[:3] if x.get("why"))
        if why and not fixes:
            fixes.append({"label": "Why nobody could cover it", "reason": why, "action": "open",
                          "payload": {"stage": "schedule", "date": d}})
    return fixes


def _floor_candidates(rows, c, v, user, b=None) -> list:
    """Who may be named as running the floor through a manager gap — the
    gate's own list (schedule_publish.cover_candidates: the roster's
    managers and owners who can work that day; TRUST-5 — it offered anyone
    on the floor then, and "Someone else…" took any typed name), the ones
    on during the gap first, then the reader when they are the account
    owner (the owner counts — ARCH-30)."""
    import schedule_publish as _sp
    import schedule_rules as _sr
    base = list((b or {}).get("cover_candidates") or [])
    if not base and c is not None:
        base = _sp.cover_candidates(c, rows, (v or {}).get("date"))
    on = []
    if v:
        d = v.get("date")
        gs, ge = v.get("gap_start"), v.get("gap_end")
        for r in rows:
            if r.get("date") != d or not (r.get("employee") or "").strip():
                continue
            sp = _sr._span(r) if hasattr(_sr, "_span") else None
            if sp and gs is not None and ge is not None and not (sp[0] < int(ge) and int(gs) < sp[1]):
                continue
            on.append(_low(r["employee"]))
    names = sorted(base, key=lambda n: (_low(n) not in on, _low(n)))
    if isinstance(user, dict):
        import schedule_versions as _sv
        me = ((user.get("name") or user.get("username") or "").strip())
        if me and _sv.authority_of(user) in _sp.LEGAL_ACK_AUTHORITIES and _low(me) not in {_low(n) for n in names}:
            names.append(me)
    return names[:12]


def _role_pool(rid, rows, role_low, exclude_low, memo=None) -> list:
    """The roster people who may work `role_low` (their roles on file or
    the roles they work this week), but `exclude_low`. The roles are read
    once per review (`memo`): staff_settings.roles_for per person per item
    was 3 of the 4.4 seconds a 375-shift review took (TRUST-26)."""
    import staff_settings as _ss
    memo = memo if memo is not None else {}
    if "roster" not in memo:
        memo["roster"] = [(e.get("name") or "").strip() for e in _ss.roster(rid) if (e.get("name") or "").strip()]
        memo["roles"] = {}
        memo["week_roles"] = {}
        for r in rows:
            memo["week_roles"].setdefault(_low(r.get("employee")), set()).add((r.get("role") or "").strip().lower())
    pool = []
    for name in memo["roster"]:
        if _low(name) == exclude_low:
            continue
        if role_low:
            if name not in memo["roles"]:
                memo["roles"][name] = set(_ss.roles_for(rid, name))
            known = memo["roles"][name] | memo["week_roles"].get(_low(name), set())
            known.discard("")
            if known and role_low not in known:
                continue
        pool.append(name)
    return pool


def _ot_added(c, name, have, add) -> float:
    """Hours past `name`'s overtime line that taking `add` more hours on top
    of `have` would create (schedule_rules.overtime_line) — 0 for a
    salaried person or when it cannot be told."""
    try:
        import schedule_rules as _sr
        if c is None or c.is_salaried(name):
            return 0.0
        line = float(_sr.overtime_line(c, name))
    except Exception:
        return 0.0
    return round(max(0.0, have + add - line) - max(0.0, have - line), 2)


def _replacement_fixes(rid, rows, c, index, prepared, scores, k=2, memo=None, offered=None) -> list:
    """Up to `k` people who could take rows[index] outright — legal by every
    rule the generation is checked against (schedule_engine.
    replacement_is_legal) — as one-tap edits, ranked by what each would
    COST the week (TRUST-14): no new overtime first, then someone not
    already named for another item (two items' fixes no longer double-book
    one person), then the rating. The side effect is said on the button
    ("+2.5h overtime"), never a bare "legal". The searches stop at the
    review's deadline (`memo`, FIX_SECONDS)."""
    import schedule_engine as _se
    if not (0 <= index < len(rows)):
        return []
    memo = memo if memo is not None else {}
    offered = offered if offered is not None else {}
    target = rows[index]
    role_low = (target.get("role") or "").strip().lower()
    pool = _role_pool(rid, rows, role_low, _low(target.get("employee")), memo)
    th = _hours(target)
    hours = memo.setdefault("hours", {})
    if not hours:
        for r in rows:
            hours[_low(r.get("employee"))] = hours.get(_low(r.get("employee")), 0.0) + _hours(r)

    def cost(n):
        have = hours.get(_low(n), 0.0)
        return (_ot_added(c, n, have, th) > 0, _low(n) in offered, -(scores.get(n) or 0), n)
    pool.sort(key=cost)
    out = []
    deadline = memo.get("deadline")
    for name in pool[:REPLACEMENT_CHECKS]:
        if deadline is not None and _time.monotonic() > deadline:
            break
        ok, _why = _se.replacement_is_legal(rid, rows, index, name, constraints=c, prepared=prepared)
        if not ok:
            continue
        have = hours.get(_low(name), 0.0)
        ot = _ot_added(c, name, have, th)
        bits = ["legal"]
        if scores.get(name):
            bits.append(f"rated {scores[name]}/5")
        bits.append(f"{have + th:g}h this week")
        if ot > 0:
            bits.append(f"+{ot:g}h overtime")
        out.append({"label": f"Give it to {name} (" + ", ".join(bits) + ")", "overtime_hours": ot or 0,
                    "employee": name, "action": "edit_rows",
                    "payload": {"change": [{"match": _match(target), "set": {"employee": name}}]}})
        offered[_low(name)] = offered.get(_low(name), 0.0) + th
        if len(out) >= k:
            break
    return out


def _floor_fixes(rid, rows, c, v) -> list:
    """One legal shift that lifts a short day (the floors stage's own pick,
    schedule_engine.crew_add_suggestion — A's Crew "Add 1 this week")."""
    import schedule_engine as _se
    role = v.get("floor_role") or v.get("role")
    d = v.get("date")
    if not role or not d:
        return []
    part = v.get("daypart") or "night"
    got = _se.crew_add_suggestion(rid, rows, d, role, part, constraints=c)
    if not got:
        return []
    r, hours_after = got
    nr = _row_out(r)
    return [{"label": f"Add {nr['employee']} {nr['shift_start']}–{nr['shift_end']} ({hours_after:g}h this week)",
             "action": "edit_rows", "payload": {"add": [nr]}}]


# ── 2. decide ──────────────────────────────────────────────────────────────

_DECIDE_OPTIONS = {
    "hours_over": [("Keep it this week", "week", "acknowledge")],
    "notice_short": [("Send it with less notice — my own rule", "week", "acknowledge")],
    "late_change": [("Send the change", "week", "acknowledge")],
    "quality_unchecked": [("I've read the week — it can go", "week", "acknowledge")],
    "saved": [("I've read it", "week", "acknowledge")],
}


def _decide(rid, gate, keep, crew, left_off, memo=None) -> list:
    import schedule_publish as _sp
    import schedule_overrides as _so
    memo = memo or {}
    held = memo.get("held") or {}
    rows, c = keep.get("rows") or [], keep.get("constraints")
    week_end = (keep.get("row") or {}).get("week_end") or (keep.get("row") or {}).get("week_start")
    out = []
    for b in gate.get("blockers") or []:
        if _sp.blocker_tier(b) != _sp.DECIDE:
            continue
        kind = _so.kind_of(b["key"])
        head = kind.split(":", 1)[0]
        opts = _DECIDE_OPTIONS.get(head) or [("Send it as it is", "week", "acknowledge")]
        if head == "rule":
            # The restaurant's own limit that is not law (TRUST-8): a
            # decision, fixed by the hard-rule pass or kept this week.
            opts = [("Keep it this week — my own limit", "week", "acknowledge")]
        options = [{"label": lab, "scope": scope, "action": act,
                    "payload": ({"key": b["key"]} if act == "acknowledge" else {})} for lab, scope, act in opts]
        text = b["text"]
        if head == "hours_over":
            if (b.get("over") or {}).get("dollars"):
                text += f" — about ${b['over']['dollars']:,} a week."
            # Its own trim, always against the budget, priced before it is
            # applied (TRUST-27: "Trim it" ran the score optimizer, which
            # trims only when trim_to_budget is on and may add hours).
            trim = _safe(lambda: trim_preview(rid, keep), None, "trim preview", rid)
            if trim:
                options.insert(0, trim)
            if b.get("beyond_accepted"):
                ba = b["beyond_accepted"]
                if ba.get("accepted") is not None:
                    unit = "$" if ba.get("unit") == "$" else ""
                    tail = "h" if ba.get("unit") == "h" else ""
                    text += (f" It is bigger than any overage you've sent past ({unit}{ba['accepted']:,.0f}{tail}), "
                             "so it holds the send.")
        elif head == "rule":
            options.insert(0, {"label": "Fix it (shows each change)", "scope": "week", "action": "apply_fixes",
                               "payload": {}})
            text += " — your own limit, not a law here."
        out.append({"key": b["key"], "kind": kind, "cls": b.get("cls"), "text": text, "holds": True,
                    "options": options, "overridden_before": b.get("overridden_before")})
    # Crew: a shift leaner than the usual crew, unanswered (A's Crew cells,
    # people on at once) — the owner's answer is written where the engine
    # reads it (crew_answers_json / role_floors_json), the scope on the button.
    for cell in (_crew_under(crew) or [])[:CREW_DECIDE]:
        out.append(_crew_decision(rid, keep, crew, cell))
    # A floor the work never needed (G2, MARKET-33): the evidence, with the
    # change the owner can make — written to their own role floors. A floor
    # the owner said to keep is not offered again until the evidence behind
    # it changes (TRUST-17).
    for f in _safe(lambda: __import__("staffing_model").floor_suggestions(rid), [], "floor suggestions", rid)[:3]:
        d = _floor_decision(rid, f)
        if d and not _so.dismissed_now(held, d["key"], d.get("evidence")):
            out.append(d)
    # A regular left with no shift (D-37), with the first reason (TRUST-13);
    # and anyone left off while their role is short on a day they could
    # work — said as that, never as "all filled".
    for p in left_off:
        key = f"left_off:{_low(p['name'])}"
        if _so.dismissed_now(held, key):
            continue
        if p.get("kind") == "regular" or p.get("cause") == "short_unused":
            opts = []
            add = p.get("add")
            if add:
                opts.append({"label": f"Add {p['name']} {add['shift_start']}–{add['shift_end']} on "
                                      f"{_weekday(add['date'])[:3]}", "scope": "week", "action": "edit_rows",
                             "payload": {"add": [add]}})
            opts.append({"label": f"Find {p['name']} a shift", "scope": "week", "action": "open",
                         "payload": {"stage": "schedule", "employee": p["name"]}})
            opts.append({"label": "Leave them off this week", "scope": "week", "action": "dismiss",
                         "payload": {"key": key, "scope": "week", "until": week_end}})
            end = "" if str(p["reason"]).rstrip().endswith(("?", ".")) else "."
            text = (f"{p['name']} usually works about {p.get('usual_hours'):g}h and has no shifts: {p['reason']}{end}"
                    if p.get("kind") == "regular" and p.get("usual_hours") else f"{p['reason']}{end}")
            out.append({"key": key, "kind": "left_off", "text": text, "holds": False, "options": opts})
    return [d for d in out if d]


def trim_preview(rid, keep):
    """The hourly budget's own trim for the rows as they stand
    (schedule_economics.trim_to_budget, always WITH the budget, in dollars
    when the week can be priced — the generation's trim), as one edit_rows
    option that says what it cuts before it is applied: "Trim $412 — 3
    shift ends, 1 shift". None when nothing can be trimmed (TRUST-27)."""
    import schedule_economics as _econ
    import schedule_rules as _sr
    row, rows, c = keep.get("row") or {}, keep.get("rows") or [], keep.get("constraints")
    if c is None or not rows:
        return None
    econ = _econ_of(row)
    try:
        hb = float(row.get("hours_budget") or 0)
    except (TypeError, ValueError):
        hb = 0.0
    lbd = float(econ.get("labor_budget_dollars") or 0)
    if hb <= 0 and lbd <= 0:
        return None
    pricing = _pricing(rid, c, econ)

    def cost(rs):
        return float(_econ.price_week(_sr.with_assumed_stays(rs, c), pricing)["total"] or 0)
    before = cost(rows)
    new, trimmed, _h = _econ.trim_to_budget(
        [dict(r) for r in rows], hb or 1.0, econ.get("daily_target_hours") or {}, constraints=c,
        floors=getattr(c, "role_floors", None), requirements=None,
        cost_fn=cost if lbd > 0 else None, budget_value=lbd or None, pricing=pricing if lbd > 0 else None)
    if not trimmed:
        return None
    cut = round(before - cost(new))
    change, remove = [], []
    for t in trimmed:
        m = {"shift_id": t.get("shift_id"), "employee": t.get("employee") or "", "date": t.get("date") or "",
             "shift_start": t.get("shift_start") or ""}
        if t.get("kind") == "removed":
            remove.append(m)
        elif t.get("to"):
            change.append({"match": m, "set": {"shift_end": t["to"]}})
    if not (change or remove):
        return None
    what = ", ".join(x for x in (f"{len(change)} shift {_plural(len(change), 'end')}" if change else "",
                                 f"{len(remove)} {_plural(len(remove), 'shift')} off" if remove else "") if x)
    return {"label": (f"Trim ${cut:,} — {what} (shows each change)" if cut > 0 else
                      f"Trim it — {what} (shows each change)"),
            "scope": "week", "action": "edit_rows", "dollars": cut if cut > 0 else None,
            "payload": {"change": change, "remove": remove}}


def _floor_decision(rid, f):
    """One owner floor the work never needed, as a decision: lower it on that
    weekday and meal (the role's floors as stored, with that one day changed
    — crew_matrix's own writer of a day's floor), or keep it — a standing
    answer kept server-side, tied to the evidence (TRUST-17)."""
    import schedule_rules as _sr
    import schedule_time as _st
    from models import get_restaurant
    role, day, part = f["role"], f["day"], f["daypart"]
    pw = _st.part_word(part, capital=False)
    key = f"floor:{role.lower()}:{day}:{part}"
    evidence = floor_evidence(f)
    options = []
    try:
        import crew_matrix as _cm
        floors = _sr.role_floors(get_restaurant(rid)) or {}
        new = _cm._put_floor(floors, role, day, part, {"present": True, "value": int(f["suggest"])})
        name = _cm._floor_name(new, role) or role
        options.append({"label": f"Lower it to {f['suggest']} on {day} {pw}s", "scope": "every", "action": "post",
                        "payload": {"path": "labor/rules", "body": {"role_floors": {name: new.get(name)}}}})
    except Exception as e:
        _capture(e, "floor decision", rid)
    options.append({"label": f"Keep {f['floor']}", "scope": "every", "action": "dismiss",
                    "payload": {"key": key, "scope": "every", "evidence": evidence}})
    return {"key": key, "kind": "floor", "text": f.get("text"), "holds": False, "options": options,
            "evidence": evidence}


def floor_evidence(f) -> str:
    """The signature of what a floor suggestion stands on: the floor and
    the most the work earned (the suggestion). A "Keep" answer holds while
    it is the same — a new busiest night that changes what the work needed,
    or a floor the owner changed, brings it back (TRUST-17); a week of the
    same evidence does not."""
    return f"floor={f.get('floor')}|suggest={f.get('suggest')}"[:200]


def _crew(rid, row, rows):
    import crew_matrix as _cm
    if rows is not None:
        return _cm.build(rid, history_id=row["id"], rows=rows)
    return _cm.cached_build(rid, history_id=row["id"])


def _crew_under(crew) -> list:
    """Crew cells where the draft runs leaner than the usual crew and the
    owner has not answered, most short first."""
    cells = []
    for r in (crew or {}).get("roles") or []:
        for cell in r.get("cells") or []:
            if cell.get("status") == "under" and not cell.get("answer"):
                cells.append(dict(cell, role=r["role"]))
    cells.sort(key=lambda x: (-(int(x.get("usual") or 0) - int(x.get("draft") or 0)), x.get("date") or ""))
    return cells


def _crew_decision(rid, keep, crew, cell) -> dict:
    import schedule_time as _st
    role, day, part = cell["role"], cell["day"], cell["part"]
    pw = _st.part_word(part, capital=False)
    busy = {"busy": " (a busy shift for its sales)", "slow": " (a slow shift for its sales)"}.get(cell.get("demand"), "")
    text = (f"{day} {pw} has {cell.get('draft')} {role} on at once; you usually run {cell.get('usual')}{busy}.")
    hid = (crew or {}).get("history_id")
    body = {"role": role, "day": day, "part": part, "history_id": hid}
    options = []
    try:
        import schedule_engine as _se
        if cell.get("date"):
            got = _se.crew_add_suggestion(rid, keep.get("rows") or [], cell["date"], role, part,
                                          constraints=keep.get("constraints"))
            if got:
                r, hrs = got
                nr = _row_out(r)
                options.append({"label": f"Add {nr['employee']} {nr['shift_start']}–{nr['shift_end']} ({hrs:g}h this "
                                         f"week)", "scope": "week", "action": "edit_rows", "payload": {"add": [nr]}})
    except Exception as e:
        _capture(e, "crew add", rid)
    options.append({"label": f"{cell.get('draft')} is fine on {day} {pw}s", "scope": "every", "action": "post",
                    "payload": {"path": "labor/crew-matrix", "body": dict(body, answer="lean")}})
    options.append({"label": f"Keep {cell.get('usual')} every {day} {pw}", "scope": "every", "action": "post",
                    "payload": {"path": "labor/crew-matrix", "body": dict(body, answer="hold")}})
    return {"key": f"crew:{role.lower()}:{day}:{part}", "kind": "crew", "text": text, "holds": False,
            "options": options}


# ── groups (TRUST-15) ──────────────────────────────────────────────────────

_DAY_KINDS = ("no_manager", "coverage_floor", "nobody_at_close", "keyholder_until_close", "owner_rule")


def group_items(items, tier) -> list:
    """The must-fix (or holding decide) items as one card per rule kind
    (TRUST-15: a 375-shift week was 51 cards, each with the same buttons and
    its own reason): {kind, tier, count, keys, text, cls, law,
    override_allowed, fixes} — the group's one fix (the hard-rule pass, which
    shows each change) and, for a decision, one answer for all of them; the
    items stay in `must_fix` / `decide` for per-item fixes and overrides.
    In the order of the items (severity first)."""
    import schedule_rules as _sr
    order, by = [], {}
    for it in items or []:
        k = it.get("kind") or "other"
        if k not in by:
            by[k] = []
            order.append(k)
        by[k].append(it)
    out = []
    for k in order:
        its = by[k]
        head, _s, rk = k.partition(":")
        n = len(its)
        if n == 1:
            text = its[0]["text"]
        elif head == "rule":
            label = _sr.LABELS.get(rk) or rk.replace("_", " ")
            if rk in _DAY_KINDS:
                u = len({it.get("date") for it in its if it.get("date")}) or n
                text = f"{u} {_plural(u, 'day')}: {label}"
            elif rk in PERSON_WEEK_KINDS:
                u = len({str(it.get("key") or "").split(":")[2] if str(it.get("key") or "").count(":") >= 2
                         else it.get("key") for it in its})
                text = f"{u} {_plural(u, 'person', 'people')} — {label}"
            else:
                text = f"{n} {_plural(n, 'shift')} — {label}"
        else:
            text = f"{n} × {its[0]['text']}"
        g = {"kind": k, "tier": tier, "count": n, "keys": [it["key"] for it in its], "text": text,
             "cls": its[0].get("cls"), "law": any(it.get("law") for it in its),
             "override_allowed": all(it.get("override_allowed", True) for it in its), "fixes": []}
        if head == "rule" and n > 1:
            g["fixes"].append({"label": GROUP_FIX_LABELS.get(rk) or f"Fix all {n} (shows each change)",
                               "action": "apply_fixes", "payload": {}})
        if tier == "decide" and n > 1:
            lab = ("Keep them this week — my own limit" if head == "rule" else
                   (its[0].get("options") or [{}])[-1].get("label") or "Send them as they are")
            g["fixes"].append({"label": lab, "action": "acknowledge_all", "payload": {"keys": g["keys"]}})
        out.append(g)
    return out


def _always_overridden(rid, gate, c) -> list:
    """The warnings this owner sends past nearly every week
    (schedule_overrides.annotate's `always_overridden`), each said and with
    a one-tap change to the rule where a setting stands behind it
    (TRUST-10: computed every check and rendered nowhere): {kind, label,
    weeks, of, text, actions}. A law-backed setting is never offered to
    loosen — it is the law."""
    import schedule_rules as _sr
    out = []
    comp = (getattr(c, "compliance", None) or {}) if c is not None else {}
    for s in gate.get("always_overridden") or []:
        kind = s.get("kind") or ""
        head, _sep, rk = kind.partition(":")
        label = (_sr.LABELS.get(rk) if head == "rule" else None) or s.get("label") or kind
        text = (f"You've sent past “{label}” in {s.get('overridden')} of {s.get('shown')} weeks"
                + (f", last on {s['last_overridden']}" if s.get("last_overridden") else "") + " — change the rule?")
        actions = []
        setting = _sr.POLICY_SETTING.get(rk) if head == "rule" else None
        if setting and c is not None and not _sr.law_backed(c, setting):
            cur = comp.get(setting) if comp.get(setting) is not None else _sr.DEFAULTS.get(setting)
            try:
                cur = float(cur)
            except (TypeError, ValueError):
                cur = None
            if cur is not None:
                step = {"max_consecutive_days": 1, "max_shift_hours": 1, "weekly_hours_ceiling": 5,
                        "min_rest_hours": -1}.get(setting)
                if step:
                    new = cur + step
                    if new > 0:
                        words = {"max_consecutive_days": f"Allow {new:g} days in a row",
                                 "max_shift_hours": f"Allow shifts up to {new:g}h",
                                 "weekly_hours_ceiling": f"Raise the weekly ceiling to {new:g}h",
                                 "min_rest_hours": f"Ask for {new:g}h rest between shifts"}[setting]
                        actions.append({"label": words, "scope": "every", "action": "post",
                                        "payload": {"path": "labor/rules",
                                                    "body": {"rules": {setting: int(new) if float(new).is_integer()
                                                                       else new}}}})
        if head == "hours_over":
            actions.append({"label": "Change the hourly budget", "scope": "every", "action": "open",
                            "payload": {"stage": "setup", "panel": "budget"}})
        actions.append({"label": "Review the rule", "scope": "every", "action": "open",
                        "payload": {"stage": "setup", "panel": "rules"}})
        out.append({"kind": kind, "label": label, "weeks": s.get("overridden"), "of": s.get("shown"),
                    "hard": s.get("hard"), "text": text, "actions": actions})
    return out


# ── FYI ────────────────────────────────────────────────────────────────────

def _fyi(gate, viols) -> list:
    """Worth a look, never a hold: the gate's notes, then the soft rules
    grouped by kind (and by day where a kind is about days), each group one
    line; reminder kinds folded to one line (TRUST-22)."""
    import schedule_rules as _sr
    out = [{"key": n.get("key"), "kind": "note", "text": n.get("text"), "count": 1} for n in gate.get("notes") or []]
    groups = {}
    for v in viols or []:
        if v.get("hard") or v.get("kind") in _sr.ROSTER_BLOCKERS:
            continue
        groups.setdefault(v["kind"], []).append(v)
    for kind, vs in sorted(groups.items(), key=lambda kv: (kv[0] in REMINDER_KINDS, -len(kv[1]), kv[0])):
        # A breach named on every row of a person's week is one PERSON
        # (TRUST-11: "2 shifts past the weekly ceiling" meant 2 people);
        # daily overtime is one per person and day.
        weekly = kind in PERSON_WEEK_KINDS
        if weekly:
            n = len({(_low(v.get("employee")), v.get("bucket")) for v in vs})
            noun = ("person", "people")
        elif kind == "daily_ot":
            n = len({(_low(v.get("employee")), v.get("date")) for v in vs})
            noun = ("shift", "shifts")
        else:
            n = len(vs)
            noun = ("shift", "shifts")
        label = _sr.LABELS.get(kind) or kind.replace("_", " ")
        if kind in REMINDER_KINDS:
            text = f"{n} {_plural(n, *noun)}: {label}."
        elif n == 1:
            text = _sr.breach_text(vs[0])
        else:
            # The days, in the week's order (never alphabetical) — and none
            # for a weekly limit, where the days say nothing (TRUST-11).
            days = [] if weekly else _days_in_order(vs)
            text = (f"{n} {_plural(n, *noun)} {label}" + (f" ({', '.join(d[:3] for d in days[:4])}"
                                                          + ("…" if len(days) > 4 else "") + ")" if days else "")
                    + f" — e.g. {_sr.breach_text(vs[0])}")
        out.append({"key": f"soft:{kind}", "kind": kind, "text": text, "count": n,
                    "unit": "people" if weekly else "shifts"})
    return out


def _days_in_order(vs) -> list:
    """The weekdays a set of breaches fall on, in the order of their dates —
    the restaurant's own week (a Wednesday-start week reads Wed … Tue),
    never alphabetical (TRUST-11)."""
    first = {}
    for v in vs:
        d = str(v.get("date") or "")[:10]
        if not d:
            continue
        name = v.get("day") or _weekday(d)
        if name and (name not in first or d < first[name]):
            first[name] = d
    return sorted(first, key=lambda n: first[n])


# ── 3. numbers ─────────────────────────────────────────────────────────────

def _empty_numbers() -> dict:
    return {"labor_pct": None, "cost": None, "overtime_hours": None,
            "overtime": {"hours": None, "premium": None, "priced": False, "text": "Priced once the week can be priced"},
            "forecast_sales": None, "savings": None, "vs_last_plan": None, "last_week": None,
            "labor_missing": None}


def price_rows(rid, row, rows, c) -> tuple:
    """(economics_json dict, priced cost) for the rows as they stand —
    the generation's own pricing (schedule_economics.week_pricing), with an
    hourly closing manager's assumed stay priced in (TRUST-21)."""
    import schedule_economics as _econ
    import schedule_rules as _sr
    try:
        econ = json.loads(row.get("economics_json") or "null") or {}
    except Exception:
        econ = {}
    if c is None or not rows:
        return econ, None
    return econ, _econ.price_week(_sr.with_assumed_stays(rows, c), _pricing(rid, c, econ))


def _pricing(rid, c, econ):
    """The week's pricing (schedule_economics.week_pricing) with the
    restaurant's hourly rate where no blended rate was kept — one pricing
    for the numbers and the budget trim's preview."""
    import schedule_economics as _econ
    pricing = _econ.week_pricing(rid, c, blended_rate=(econ or {}).get("blended_rate") or None)
    if not pricing.get("blended_rate"):
        from models import get_restaurant
        r = get_restaurant(rid)
        pricing["blended_rate"] = getattr(r, "hourly_rate", None) or 15.0
    return pricing


def forecast_record(rid) -> dict:
    """The frozen weekly sales projections' record (forecast_log
    revenue_week): {weeks, within_pct (the FORECAST_BAND share of misses
    were at or under it), weeks_within, mean_error_pct, last: {projected,
    actual, period}} — None below FORECAST_MIN_WEEKS scored weeks."""
    import forecast_log
    rows = forecast_log._scored_periods(rid, "revenue_week", forecast_log.DB_PATH)
    errs = []
    for r in rows:
        try:
            errs.append(abs(float(r["error_pct"])))
        except (TypeError, ValueError, KeyError):
            continue
    if len(errs) < FORECAST_MIN_WEEKS:
        return None
    import math
    srt = sorted(errs)
    # The miss FORECAST_BAND of the scored weeks came in at or under.
    band = max(1.0, math.ceil(srt[max(0, math.ceil(FORECAST_BAND * len(srt)) - 1)]))
    last = rows[0] if rows else None
    # Against a naive guess (blind re-audit 10/11/26 TRUST-30): "within 12%
    # on 7 of 9 weeks" sounded good even where "same as last week" would
    # have been within 8%. The weeks this projection was nearer the actual
    # than last week's sales were, and forecast_log.skill (the lower of its
    # skill against last week and against the recent average — it must beat
    # both); the range is withheld when it is not positive (_numbers).
    sk = forecast_log.skill(rows)
    beat, vs = 0, 0
    for r in rows:
        p, a, n = forecast_log._f(r.get("predicted")), forecast_log._f(r.get("actual")), \
            forecast_log._f(r.get("naive_last"))
        if None in (p, a, n):
            continue
        vs += 1
        if abs(p - a) < abs(n - a):
            beat += 1
    return {"weeks": len(errs), "within_pct": int(band), "weeks_within": sum(1 for e in errs if e <= band),
            "mean_error_pct": round(sum(errs) / len(errs), 1),
            "skill": sk.get("skill"), "skill_pct": sk.get("skill_pct"),
            "skill_vs_last": sk.get("skill_vs_last"), "skill_vs_mean": sk.get("skill_vs_mean"),
            "n_skill": sk.get("n_skill"), "beats_last_week": beat, "vs_last_week": vs,
            "last": ({"projected": last.get("predicted"), "actual": last.get("actual"),
                      "period": last.get("horizon_end")} if last else None)}


def forecast_skill_line(rec) -> str | None:
    """The forecast's record against a naive guess, in words (TRUST-30):
    "Beats last week's sales as a guess on 5 of 8 weeks" — None until the
    naive figure was recorded for FORECAST_MIN_WEEKS weeks."""
    if not rec or int(rec.get("vs_last_week") or 0) < FORECAST_MIN_WEEKS:
        return None
    line = (f"Beats last week's sales as a guess on {rec['beats_last_week']} of the last "
            f"{rec['vs_last_week']} weeks")
    if rec.get("skill") is not None and rec["skill"] <= 0:
        line += " — no better than a naive guess, so no range is shown"
    return line + "."


def forecast_has_skill(rec) -> bool:
    """False when the forecast's measured skill is zero or below (it does
    no better than repeating last week, or the recent average): the labor
    range and its record are not stated then (TRUST-30). True while the
    skill is not measured yet — the plain miss record still stands."""
    if not rec:
        return False
    return rec.get("skill") is None or float(rec["skill"]) > 0


def _numbers(rid, row, rows, c, dates, quality=None) -> dict:
    import schedule_economics as _econ
    econ, cost = price_rows(rid, row, rows, c)
    out = _empty_numbers()
    rev = econ.get("projected_revenue")
    if rev:
        out["forecast_sales"] = {"value": round(float(rev)),
                                 "basis": econ.get("projected_revenue_source") or "the forecast this week was built on"}
        # The figure the crew was sized on and the one Home states, with the
        # schedule forecast's estimator and range (ARCH-28, additive).
        fb = econ.get("forecast_basis") or {}
        if fb:
            out["forecast_sales"].update(sized_on=fb.get("sized_on"), surface=fb.get("surface"),
                                         estimator=fb.get("estimator"), low=fb.get("low"), high=fb.get("high"),
                                         reconcile=fb.get("text"))
    if cost:
        priced = "overtime_hours" in cost
        ot = cost.get("overtime_hours") if priced else None
        out["overtime_hours"] = ot
        prem = cost.get("overtime_premium")
        out["overtime"] = {"hours": ot, "premium": prem, "priced": priced,
                           "text": ("Not priced for this week" if not priced else
                                    (f"{ot:g}h of overtime, ${prem:,.0f} in premium pay" if ot else "No overtime"))}
    lv = None
    if cost and rev:
        lv = _econ.labor_view(rid, cost, rev, labor_target=_labor_target(rid),
                              labor_budget_dollars=econ.get("labor_budget_dollars"), week_dates=dates,
                              quality=quality)
    rec = _safe(lambda: forecast_record(rid), None, "forecast_record", rid)
    if lv:
        pct = lv.get("pct")
        low = high = None
        track = None
        skill_line = forecast_skill_line(rec)
        if rec and pct and rev and forecast_has_skill(rec):
            e = rec["within_pct"] / 100.0
            low, high = round(lv["cost"] / (float(rev) * (1 + e)) * 100, 1), \
                (round(lv["cost"] / (float(rev) * (1 - e)) * 100, 1) if e < 1 else None)
            track = (f"Our sales forecasts here were within {rec['within_pct']}% on {rec['weeks_within']} of the last "
                     f"{rec['weeks']} weeks" + (f"; {skill_line[0].lower()}{skill_line[1:]}" if skill_line else "."))
        elif rec and skill_line:
            # No better than a naive guess: the range is withheld and the
            # reason is the record line (TRUST-30).
            track = skill_line
        out["labor_pct"] = {"value": pct, "low": low, "high": high, "basis": lv.get("basis"), "label": lv.get("label"),
                            "target": lv.get("target_pct"), "target_basis": lv.get("target_basis"),
                            "grade": lv.get("grade") or _econ.labor_grade(pct, lv.get("target_pct")),
                            "track_record": track, "text": lv.get("text"),
                            # Against a naive guess (TRUST-30, additive).
                            "forecast_skill": ({"skill": rec.get("skill"), "skill_pct": rec.get("skill_pct"),
                                                "beats_last_week": rec.get("beats_last_week"),
                                                "vs_last_week": rec.get("vs_last_week"),
                                                "range_withheld": not forecast_has_skill(rec)} if rec else None),
                            "other_basis_note": lv.get("other_basis_note")}
        out["cost"] = {"hourly": lv.get("hourly_cost"), "salaried": lv.get("salaried_cost"), "total": lv.get("cost"),
                       "text": (f"${lv.get('hourly_cost'):,.0f} hourly"
                                + (f" + ${lv['salaried_cost']:,.0f} salaried" if lv.get("salaried_cost") else ""))}
        # Scheduled against scheduled (blind re-audit 10/11/26 TRUST-7):
        # `savings` stays null (shipped clients decode it); the comparison
        # with the last published week's plan is `vs_last_plan`, withheld
        # while the draft is short of what its shifts need.
        if lv.get("vs_last_plan"):
            out["vs_last_plan"] = lv["vs_last_plan"]
    elif cost:
        out["cost"] = {"hourly": cost.get("total"), "salaried": None, "total": cost.get("total"),
                       "text": f"${cost.get('total') or 0:,.0f} hourly"}
    if not out["labor_pct"]:
        out["labor_missing"] = _safe(lambda: labor_missing(rid, cost, rev, dates), None, "labor missing", rid)
    if rec and rec.get("last") and rec["last"].get("projected") and rec["last"].get("actual"):
        p, a = float(rec["last"]["projected"]), float(rec["last"]["actual"])
        diff = (a - p) / p * 100 if p else 0
        out["last_week"] = {"projected": round(p), "actual": round(a),
                            "text": (f"The week ending {_mdy(rec['last']['period'])} we projected ${p:,.0f} in sales; "
                                     f"you ran ${a:,.0f} ({'+' if diff >= 0 else '−'}{abs(diff):.0f}%).")}
    return out


LABOR_MISSING_ACTION = {"label": "Enter a weekly sales estimate", "action": "open",
                        "payload": {"stage": "setup", "panel": "targets"}}


def labor_missing(rid, cost, rev, dates) -> dict:
    """Why the week has no labor % and the fix that would give it one
    (schedule re-audit 10/11/26 TRUST-29: both clients said "generate a new
    draft to see it" to an owner with no sales at all, where regenerating
    cannot produce a forecast and loses their edits). {reason, text,
    action?}: "not_priced" — the rows have no cost yet; "forecast_since" —
    a forecast (or the owner's weekly sales estimate) exists now that the
    draft was built without, so generating the week again does measure it;
    "too_little_history" — some sales, fewer than the three complete weeks a
    forecast needs; "no_sales" — nothing connected. The last two offer the
    weekly sales estimate (the targets), the one fix that works today."""
    import schedule_economics as _econ
    from models import get_restaurant, weekly_revenue_target
    if not cost:
        return {"reason": "not_priced",
                "text": "The week couldn't be priced yet — add pay rates in Team (or an hourly rate in your targets) "
                        "to see labor as a share of sales."}
    if rev:
        return {"reason": "not_priced", "text": "Labor couldn't be worked out against this week's sales forecast."}
    now = _econ.projected_weekly_revenue(rid, week_dates=dates or None) or {}
    r = get_restaurant(rid)
    est = float(weekly_revenue_target(r) or 0) if r is not None else 0.0
    if now.get("value") or est > 0:
        v = float(now.get("value") or est)
        src = now.get("source") if now.get("value") else "your weekly sales estimate"
        return {"reason": "forecast_since",
                "text": (f"This draft was built before there was a sales forecast for the week — there is one now "
                         f"(${v:,.0f}, from {src}). Generating the week again measures labor against it.")}
    weeks = int(now.get("weeks") or 0)
    if weeks > 0 or _has_sales(rid):
        return {"reason": "too_little_history",
                "text": (f"Only {weeks} complete {_plural(weeks, 'week')} of sales on file — a forecast needs three. "
                         "Enter a weekly sales estimate in your targets until then, and labor shows against it."),
                "action": dict(LABOR_MISSING_ACTION)}
    return {"reason": "no_sales",
            "text": ("No sales are connected yet, so there's no forecast to measure labor against. Connect your POS, "
                     "or enter a weekly sales estimate in your targets."),
            "action": dict(LABOR_MISSING_ACTION)}


def _has_sales(rid) -> bool:
    from models import get_conn as _gc
    conn = _gc()
    try:
        row = conn.execute("SELECT 1 FROM labor_daily_history WHERE restaurant_id=? AND sales > 0 LIMIT 1",
                           (rid,)).fetchone()
    finally:
        conn.close()
    return row is not None


def _labor_target(rid):
    """The ONE read of the labor target (thresholds.target_for: the owner's
    goal first, else the set or starting target) - never the bare column
    (tests/test_targets_published_figures ratchet)."""
    from models import get_restaurant
    r = get_restaurant(rid)
    if r is None:
        return None
    try:
        import thresholds
        return thresholds.target_for(r, "labor")["pct"]
    except Exception:
        return None


# ── the quality score, graded against the restaurant's own weeks ───────────

def _empty_quality() -> dict:
    return {"score": None, "band": None, "grade": "ungraded", "percentile": None, "weeks_compared": 0,
            "lead": None, "text": None, "measured_share": None, "measured_text": None, "outcome_link": None,
            "outcome_text": None}


def _quality(rid, row, quality) -> dict:
    """The score graded against this restaurant's own last QUALITY_WEEKS
    weeks (TRUST-29: coloured at fixed 80/60 lines, the structural caps put
    most weeks in amber, so the colour said nothing). `lead` is the reason
    the score was capped, said first."""
    out = _empty_quality()
    if not quality or not quality.get("checked"):
        out["text"] = "Shift Quality hasn't scored this week."
        return out
    score = quality.get("score")
    out.update(score=score, band=quality.get("band"))
    from models import get_conn
    conn = get_conn()
    try:
        prior = [float(r["quality_score"]) for r in conn.execute(
            "SELECT quality_score FROM schedule_history WHERE restaurant_id=? AND id<>? AND quality_score IS NOT NULL "
            "AND superseded_by IS NULL ORDER BY week_start DESC, id DESC LIMIT ?",
            (rid, row["id"], QUALITY_WEEKS)).fetchall() if r["quality_score"] is not None]
    finally:
        conn.close()
    out["weeks_compared"] = len(prior)
    held = quality.get("held_by") or {}
    lead = held.get("text")
    if not lead:
        below = sorted(quality.get("below_profile") or [], key=lambda b: b.get("score") or 0)
        if below:
            b = below[0]
            import schedule_time as _st
            lead = (f"Capped by {(b.get('day') or '')[:3]} {_mdy(b.get('date'))} {_st.part_word(b.get('daypart'), False)}"
                    f": {b.get('reason')}")
    out["lead"] = lead
    # What the score stands on (blind re-audit 10/11/26 LOGIC-15) and what
    # it has meant here (TRUST-31): the measured share and the band it held
    # back, and the restaurant's own high- and low-scoring weeks' outcomes
    # ("—" below the sample floor, with how many weeks so far). Additive.
    m = quality.get("measured") or {}
    out["measured_share"] = m.get("share")
    out["measured_text"] = m.get("text")
    import schedule_calibration as _cal
    link = _safe(lambda: _cal.score_outcome_link(rid), None, "score_outcome_link", rid)
    out["outcome_link"] = link
    out["outcome_text"] = (link or {}).get("text") if (link or {}).get("ready") else None
    if score is None or len(prior) < QUALITY_MIN_WEEKS:
        out["text"] = (f"{score}/100 — graded once {QUALITY_MIN_WEEKS} of your weeks are scored "
                       f"({len(prior)} so far)." if score is not None else None)
        return out
    pct = int(round(100.0 * sum(1 for p in prior if p < float(score)) / len(prior)))
    out["percentile"] = pct
    out["grade"] = "good" if pct >= QUALITY_GOOD_PCTILE else ("weak" if pct < QUALITY_WEAK_PCTILE else "typical")
    word = {"good": "better than most", "typical": "about your usual", "weak": "weaker than most"}[out["grade"]]
    out["text"] = f"{score}/100 — {word} of your last {len(prior)} weeks."
    return out


# ── 4. what changed ────────────────────────────────────────────────────────

def _changed(rid, row, rows, crew, saved_review, screen=False) -> dict:
    import schedule_versions as _sv
    out = {"vs_last_week": [], "vs_usual": [], "vs_last_draft": [], "cavnar_changes": []}
    # Against the last published week, weekday for weekday.
    try:
        import schedule_engine as _se
        prior = _se._last_published_week(rid, row["week_start"])
        prior_rows = _sv.rows_from_csv(prior.get("schedule_csv") or "") if prior else []
        if prior_rows:
            shift = (date.fromisoformat(str(row["week_start"])[:10])
                     - date.fromisoformat(str(prior["week_start"])[:10])).days
            moved = []
            for r in prior_rows:
                r = dict(r)
                try:
                    r["date"] = (date.fromisoformat(str(r.get("date"))[:10]) + timedelta(days=shift)).isoformat()
                except (TypeError, ValueError):
                    continue
                moved.append(r)
            d = _sv.diff(moved, rows)
            delta_n = len(rows) - len(prior_rows)
            head = (f"{'+' if delta_n >= 0 else '−'}{abs(delta_n)} {_plural(abs(delta_n), 'shift')}, "
                    f"{'+' if d['hours_after'] >= d['hours_before'] else '−'}"
                    f"{abs(round(d['hours_after'] - d['hours_before'], 1)):g}h against the week of "
                    f"{_mdy(prior['week_start'])}.")
            out["vs_last_week"] = [head] + [ln for ln in _sv.diff_lines(d, limit=5) if not ln.endswith("this week (")
                                            and "h this week" not in ln][:4]
        else:
            out["vs_last_week"] = ["No published week before this one to compare with."]
    except Exception as e:
        _capture(e, "changed vs last week", rid)
    # Against the usual crew (Crew's cells, people on at once).
    try:
        import schedule_time as _st
        lean, heavy = [], []
        for r in (crew or {}).get("roles") or []:
            for cell in r.get("cells") or []:
                where = f"{cell['day'][:3]} {_st.part_word(cell['part'], False)} {r['role']}"
                if cell.get("status") in ("under", "below_rule"):
                    lean.append(where)
                elif cell.get("status") == "over":
                    heavy.append(where)
        bits = []
        if lean:
            bits.append(f"{len(lean)} {_plural(len(lean), 'shift')} leaner than your usual crew ("
                        + ", ".join(lean[:3]) + ("…" if len(lean) > 3 else "") + ")")
        if heavy:
            bits.append(f"{len(heavy)} heavier (" + ", ".join(heavy[:3]) + ("…" if len(heavy) > 3 else "") + ")")
        if crew and crew.get("has_usual"):
            out["vs_usual"] = ["; ".join(bits) + "." if bits else "Every shift matches your usual crew."]
    except Exception as e:
        _capture(e, "changed vs usual", rid)
    # Against the last draft of this week: the copy this one replaced, else
    # this week's previous saved version.
    try:
        before, what = _previous_draft(rid, row, screen=screen)
        if before is not None:
            d = _sv.diff(before, rows)
            locked = sum(1 for r in rows if __import__("schedule_rows").is_locked(r))
            line = (f"Against {what}: {d['changes']} {_plural(d['changes'], 'shift')} changed"
                    + (f"; your {locked} pinned {_plural(locked, 'shift')} kept" if locked else "") + ".")
            out["vs_last_draft"] = [line] + (_sv.diff_lines(d, limit=3, unchanged="") if d["changes"] else [])
            out["vs_last_draft"] = [x for x in out["vs_last_draft"] if x]
    except Exception as e:
        _capture(e, "changed vs last draft", rid)
    # What Cavnar AI changed after the first draft (the repair passes'
    # own lines, kept with the review).
    try:
        fixes = [f for f in (saved_review.get("fixes") or []) if isinstance(f, dict)]
        if fixes:
            out["cavnar_changes"] = [f"Cavnar AI changed {len(fixes)} {_plural(len(fixes), 'shift')} after the first "
                                     "draft to keep your rules."] + \
                [str(f.get("reason") or f.get("text") or "").strip() for f in fixes[:3]
                 if (f.get("reason") or f.get("text"))]
    except Exception as e:
        _capture(e, "cavnar changes", rid)
    return out


def _previous_draft(rid, row, screen=False):
    """(rows, how to name it) — with unsaved edits on screen, the saved
    week; else the draft this one replaced (superseded by it), else the
    version saved before the newest one of this week."""
    import schedule_versions as _sv
    from models import get_conn
    conn = get_conn()
    try:
        if screen:
            return _sv.latest_rows(conn, row["id"]), "the week as saved"
        # Deliberately a REPLACED row: the newest draft this one superseded.
        prev = conn.execute("SELECT id, schedule_csv FROM schedule_history WHERE id=(SELECT MAX(id) FROM "
                            "schedule_history WHERE restaurant_id=? AND superseded_by=?)", (rid, row["id"])).fetchone()
        if prev is not None and (prev["schedule_csv"] or "").strip():
            return _sv.rows_from_csv(prev["schedule_csv"]), "your last draft of this week"
        vs = conn.execute("SELECT schedule_csv FROM schedule_versions WHERE history_id=? AND restaurant_id=? "
                          "ORDER BY version DESC LIMIT 2", (row["id"], rid)).fetchall()
    finally:
        conn.close()
    if len(vs) >= 2:
        return _sv.rows_from_csv(vs[1]["schedule_csv"] or ""), "the version saved before this one"
    return None, None


def _narrative(saved_review) -> list:
    n = saved_review.get("narrative")
    if isinstance(n, str):
        return [n] if n.strip() else []
    return [str(x) for x in (n or []) if str(x).strip()]


# ── 5. where it is fragile ─────────────────────────────────────────────────

# The shift-quality dimensions whose weaknesses are a risk to the week as it
# runs (not a matter of taste): somebody new without cover, somebody likely
# to miss, the busy shifts piled on one person, a long run of days.
_FRAGILE_DIMS = {"experience_balance": "new", "reliability": "reliability", "fairness": "busy", "fatigue": "fatigue",
                 "training_balance": "training"}


def _fragile(rid, rows, c, dates, quality) -> list:
    """Where the week can break, ranked: no-show odds with the person to ask
    (schedule_learning.standby_days), the closing stays the verdict assumes
    (B's assumed_stays, priced), overtime exposure (overtime_forecast) and
    the shift-quality risks the scorer found on each shift."""
    import schedule_rules as _sr
    out = []
    try:
        import schedule_engine as _se_sb
        for d in _se_sb.standby_for_rows(rid, rows, dates, limit=2):
            pct = int(round(100 * float(d.get("chance_of_a_no_show") or 0)))
            sb = d.get("standby") or {}
            sh = d.get("standby_shift") or {}
            if sh:
                # A busy shift likely to come up short asks for a standby
                # (G2, RM-18): its expected attendance against its requirement.
                text = f"A standby is needed on {sh.get('text') or _mdy(d['date'])}."
                pct = max(pct, int(round(100 * float(sh.get("p_short") or 0))))
            else:
                text = (f"{d['day'][:3]} {_mdy(d['date'])}: about a {pct}% chance someone doesn't show (from each "
                        f"person's record; the overall no-show rate here is "
                        f"{100 * float(d.get('base_rate') or 0):.1f}%).")
            action = None
            if sb.get("employee"):
                text += f" {sb['employee']} is off and could be on call."
                action = {"label": f"Ask {sb['employee']}", "action": "post",
                          "payload": {"path": "labor/standby/ask",
                                      "body": {"date": d["date"], "employee": sb["employee"],
                                               "shift_start": sb.get("shift_start"), "shift_end": sb.get("shift_end")}}}
            elif sh:
                text += " Nobody in the role is free to be on call — add one more if the budget has room."
            out.append({"key": f"standby:{d['date']}", "kind": "standby" if sh else "no_show", "text": text,
                        "rank": (0.0 if sh else 0.2) + (1 - pct / 100.0) / 10.0, "action": action})
    except Exception as e:
        _capture(e, "standby", rid)
    if c is not None:
        try:
            # The closing stays the verdict assumes, FOLDED into one line
            # (TRUST-18: seven near-identical "assumed to stay until
            # 11:30pm" lines filled the capped list and pushed real risks —
            # 11h of overtime — off it). One action writes every stay; the
            # stay's NEW end (the old code wrote the scheduled end back).
            stays = _sr.assumed_stays(rows, c)
            if stays:
                who = {}
                for s in stays:
                    who[s.get("employee")] = who.get(s.get("employee"), 0) + 1
                mins = sorted({int(s.get("minutes") or 0) for s in stays})
                paid = sum(float(s.get("hours") or 0) for s in stays if s.get("hourly"))
                names = ", ".join(f"{n} ×{k}" if k > 1 else str(n) for n, k in who.items())
                span = f"{mins[0]} min" if len(mins) == 1 else f"{mins[0]}–{mins[-1]} min"
                text = (f"The closing manager is assumed to stay {span} past their scheduled end on {len(stays)} "
                        f"{_plural(len(stays), 'night')} ({names}) — until the last person leaves"
                        + (f"; {paid:g}h of it is counted in the labor cost." if paid else "."))
                change = []
                for s in stays:
                    p = (s.get("action") or {}).get("payload") or {}
                    until = p.get("new_end") or s.get("until")
                    if until:
                        change.append({"match": {"shift_id": None, "employee": s.get("employee"), "date": s.get("date"),
                                                 "shift_start": s.get("shift_start") or p.get("shift_start")},
                                       "set": {"shift_end": until}})
                out.append({"key": "stays", "kind": "assumed_stay", "text": text, "count": len(stays),
                            "rank": 0.65,
                            "action": ({"label": f"Schedule {_plural(len(change), 'the stay', 'each stay')} "
                                                 "(shows each change)", "action": "edit_rows",
                                        "payload": {"change": change}} if change else None)})
        except Exception as e:
            _capture(e, "assumed stays", rid)
        try:
            import schedule_learning as _sl2
            for f in _sl2.overtime_forecast(rows, c)[:3]:
                cand = f.get("candidate") or {}
                act = None
                if cand.get("employee") and cand.get("index") is not None and cand["index"] < len(rows):
                    act = {"label": f"Move a shift to {cand['employee']}", "action": "edit_rows",
                           "payload": {"change": [{"match": _match(rows[cand["index"]]),
                                                   "set": {"employee": cand["employee"]}}]}}
                try:
                    over = float(f.get("over") or f.get("over_hours") or f.get("hours_over") or 0)
                except (TypeError, ValueError):
                    over = 0.0
                # Ranked by its cost: more hours past the line, higher.
                out.append({"key": f"ot:{_low(f.get('employee'))}", "kind": "overtime", "text": f.get("text"),
                            "rank": 0.3 - min(over, 40.0) / 1000.0, "action": act})
        except Exception as e:
            _capture(e, "overtime forecast", rid)
    try:
        seen = set()
        for s in (quality or {}).get("shifts") or []:
            for dim in s.get("dimensions") or []:
                kind = _FRAGILE_DIMS.get(dim.get("key"))
                if not kind or not dim.get("weaknesses") or (dim.get("score") or 100) >= 80:
                    continue
                w = str(dim["weaknesses"][0]).strip()
                if w in seen:
                    continue
                seen.add(w)
                import schedule_time as _st
                where = f"{(s.get('day') or '')[:3]} {_st.part_word(s.get('daypart'), False)}".strip()
                out.append({"key": f"{kind}:{s.get('date')}:{s.get('daypart')}", "kind": kind,
                            "text": f"{where}: {w}", "rank": 0.7 + (dim.get("score") or 0) / 1000.0,
                            "action": {"label": "Let Cavnar AI improve it (shows each change)", "action": "optimize",
                                       "payload": {}}})
    except Exception as e:
        _capture(e, "quality risks", rid)
    out.sort(key=lambda x: x["rank"])
    return out[:8]


# ── 6. why these people, why not the others ────────────────────────────────

def _short_cells(c, viols, crew) -> list:
    """The shifts the week leaves short, by role family: a floor or a
    standing rule the sweep found unmet (coverage_floor, owner_rule) and the
    Crew cells leaner than the usual crew. [{family, role, date, part,
    text}]."""
    import schedule_time as _st
    out = []
    for v in viols or []:
        if v.get("kind") not in ("coverage_floor", "owner_rule") or not v.get("date"):
            continue
        role = v.get("floor_role") or v.get("role") or ""
        if not role:
            continue
        part = v.get("daypart") or ""
        out.append({"family": c.family(role) if hasattr(c, "family") else role.lower(), "role": role,
                    "date": v["date"], "part": part,
                    "text": f"{_weekday(v['date'])[:3]} {_mdy(v['date'])}"
                            + (f" {_st.part_word(part, False)}" if part else "")})
    for r in (crew or {}).get("roles") or []:
        for cell in r.get("cells") or []:
            if cell.get("status") in ("under", "below_rule") and cell.get("date") and not cell.get("answer"):
                out.append({"family": c.family(r["role"]) if hasattr(c, "family") else r["role"].lower(),
                            "role": r["role"], "date": cell["date"], "part": cell.get("part") or "",
                            "text": f"{_weekday(cell['date'])[:3]} {_mdy(cell['date'])} "
                                    f"{_st.part_word(cell.get('part'), False)}"})
    out.sort(key=lambda x: x["date"])
    return out


def left_off_reasons(rid, rows, c, scores=None, dates=None, viols=None, crew=None) -> list:
    """Everyone active on the roster with no shift this week, with the FIRST
    reason that kept them off (TRUST-13: the review listed bare names, so a
    GM assumed the AI forgot them): time off all week, unavailable on every
    open day, at their hours cap already, no shift in weeks, else the role's
    shifts were full with people rated higher. {name, reason, kind,
    usual_hours}.

    "All filled" is said only when it is true (re-audit 10/11/26 TRUST-13):
    a role the week leaves short (`viols` — a floor or standing rule unmet;
    `crew` — a Crew cell leaner than usual) on a day the person could work
    is said as that ("Cavnar AI didn't use Uma even though Tue 10/13 dinner
    is short of Servers — add her?", cause `short_unused`, with the add when
    the floors stage's own pick is her)."""
    if c is None:
        return []
    shorts = _safe(lambda: _short_cells(c, viols, crew), [], "short cells", rid)
    import models as _m
    scores = scores or {}
    on = {_low(r.get("employee")) for r in rows if (r.get("employee") or "").strip()}
    dates = dates or sorted({r.get("date") for r in rows if r.get("date")})
    open_dates = [d for d in dates if d not in set(getattr(c, "closed_dates", None) or ())]
    usual = _safe(lambda: _m.get_prior_shift_pattern(rid), {}, "usual pattern", rid)
    usual_low = {_low(n): v for n, v in (usual or {}).items()}
    roles = getattr(c, "roster_roles", None) or {}
    if not roles:
        import staff_settings as _ss
        roles = {e["name"]: e.get("role") or "" for e in _ss.roster(rid) if e.get("name")}
    out = []
    for name in getattr(c, "roster_names", None) or []:
        key = c.key(name) if hasattr(c, "key") else _low(name)
        if _low(name) in on or key in getattr(c, "inactive", set()):
            continue
        if c.is_salaried(name) and key in (getattr(c, "managers", None) or {}):
            continue          # the manager plan decides a salaried manager's week
        uh = (usual_low.get(_low(name)) or {}).get("avg_hours")
        reason, kind = _first_reason(c, name, key, open_dates, roles, rows, scores)
        add = None
        if kind == "role_full" and shorts:
            role = (roles.get(name) or "").strip()
            fam = c.family(role) if hasattr(c, "family") and role else role.lower()
            blocked = (getattr(c, "blocked_dates", None) or {}).get(key) or {}
            for s in shorts:
                if s["family"] != fam or s["date"] in blocked:
                    continue
                try:
                    if not c.can_work(name, s["date"])[0]:
                        continue
                except Exception:
                    continue
                reason = (f"Cavnar AI didn't use {name} even though {s['text']} is short of {s['role']} — "
                          "add them?")
                kind = "short_unused"
                try:
                    import schedule_engine as _se
                    got = _se.crew_add_suggestion(rid, rows, s["date"], s["role"], s["part"] or "night",
                                                  constraints=c)
                    if got and _low(got[0].get("employee")) == _low(name):
                        add = _row_out(got[0])
                except Exception as e:
                    _capture(e, "left-off add", rid)
                break
        out.append({"name": name, "reason": reason, "kind": "regular" if (uh or 0) >= REGULAR_HOURS and
                    kind not in ("time_off", "dormant", "unavailable") else kind,
                    "cause": kind, "usual_hours": round(float(uh), 1) if uh else None, "add": add})
    out.sort(key=lambda x: (x["kind"] != "regular", x["name"].lower()))
    return out


def _first_reason(c, name, key, open_dates, roles, rows, scores):
    blocked = (getattr(c, "blocked_dates", None) or {}).get(key) or {}
    if open_dates and all(d in blocked for d in open_dates):
        why = next(iter(blocked.values()), "") or "time off"
        span = f"{_mdy(open_dates[0])}–{_mdy(open_dates[-1])}"
        return f"time off {span} ({why})" if why and "time off" not in str(why) else f"time off {span}", "time_off"
    dormant = (getattr(c, "dormant", None) or {}).get(key)
    if dormant:
        return f"no shift since {_mdy(dormant)} — deactivate them if they've left", "dormant"
    if open_dates:
        able = [d for d in open_dates if c.can_work(name, d)[0] and d not in blocked]
        if not able:
            why = c.can_work(name, open_dates[0])[1] if open_dates else ""
            return f"not available on any open day this week ({why})" if why else \
                "not available on any open day this week", "unavailable"
    try:
        cap = float(c.max_hours(name))
        base = sum(float(h or 0) for h in ((getattr(c, "base_hours", None) or {}).get(key) or {}).values()) \
            if isinstance((getattr(c, "base_hours", None) or {}).get(key), dict) else \
            float((getattr(c, "base_hours", None) or {}).get(key) or 0)
        if cap and base >= cap - 0.5:
            return f"at their {cap:g}h cap already ({base:g}h this payroll week)", "hours_cap"
    except Exception:
        pass
    role = (roles.get(name) or "").strip()
    if role:
        fam = c.family(role) if hasattr(c, "family") else role.lower()
        mates = sorted({r.get("employee") for r in rows if (r.get("employee") or "").strip()
                        and (c.family(r.get("role") or "") if hasattr(c, "family") else (r.get("role") or "").lower())
                        == fam})
        shifts = sum(1 for r in rows if (c.family(r.get("role") or "") if hasattr(c, "family")
                                         else (r.get("role") or "").lower()) == fam)
        mine = scores.get(name)
        stronger = [m for m in mates if (scores.get(m) or 0) > (mine or 0)]
        if shifts:
            return (f"{role} shifts were all filled ({shifts} {_plural(shifts, 'shift')}, {len(mates)} "
                    f"{_plural(len(mates), 'person', 'people')})"
                    + (f"; rated {mine}/5 against {len(stronger)} rated higher" if mine and stronger else
                       ("; not rated yet" if mine is None and stronger else ""))), "role_full"
        return f"no {role} shifts were needed this week", "role_full"
    return "no role on file, so no shift could be matched to them", "no_role"


def _rkey(date_, start, employee) -> tuple:
    return (str(date_ or "")[:10], " ".join(str(start or "").split()).lower(), _low(employee))


def engine_reasons(saved_review) -> dict:
    """{(date, start, person): reason} — the engine's own reason for each
    assignment of the solve that drafted the week (contract #4): the final
    solve's reasons schedule_job kept with the draft
    (`review.assignment_reasons`, matched to the rows as saved), else the
    solver-first draft's own (`draft_source.solver.reasons`). Each reason
    {text, runner_up, facts, source}. {} when none was recorded."""
    sr = saved_review or {}
    out = {}
    kept = sr.get("assignment_reasons")
    items = (kept.get("reasons") if isinstance(kept, dict) else kept) or []
    src = (kept.get("source") if isinstance(kept, dict) else None) or "solver"
    if not items:
        items = (((sr.get("draft_source") or {}).get("solver") or {}).get("reasons")) or []
        src = "solver"
    for r in items:
        if not isinstance(r, dict) or not r.get("text") or not (r.get("employee") or "").strip():
            continue
        ru = r.get("runner_up")
        runner = (ru.get("employee") if isinstance(ru, dict) else ru) or r.get("instead")
        out[_rkey(r.get("date"), r.get("shift_start"), r.get("employee"))] = {
            "text": str(r["text"]), "runner_up": runner, "facts": r.get("facts") or [],
            "shift_id": r.get("shift_id"), "source": r.get("source") or src}
    return out


def engine_status(saved_review, rows) -> dict:
    """Whether the week's "why" can be the engine's own (TRUST-2): {recorded,
    count, matched, changed_after, text} — how many of the rows as they
    stand still carry the person the solve chose. Said plainly when nothing
    was recorded, never reconstructed and passed off as the engine's."""
    reasons = engine_reasons(saved_review)
    if not reasons:
        return {"recorded": False, "count": 0, "matched": 0, "changed_after": 0,
                "text": "This draft didn't record the engine's own reasons, so each line here compares the pick "
                        "with the best legal alternative after the draft."}
    matched = sum(1 for r in rows if _rkey(r.get("date"), r.get("shift_start"), r.get("employee")) in reasons)
    staffed = sum(1 for r in rows if (r.get("employee") or "").strip())
    changed = max(0, staffed - matched)
    text = "Each reason is the solver's own, from the solve that drafted this week."
    if changed:
        text += (f" {changed} {_plural(changed, 'shift')} changed after it — "
                 f"{'that one is' if changed == 1 else 'those are'} compared after the draft.")
    return {"recorded": True, "count": len(reasons), "matched": matched, "changed_after": changed, "text": text}


_SOURCE_WORDS = {"solver": "Cavnar AI's solver", "model": "the AI model", "carry_forward": "last week, carried forward",
                 "shapes+cp-sat": "Cavnar AI's solver"}


def draft_account(saved_review):
    """Which engine drafted the week and what the model critic kept or
    refused of its own edits (TRUST-2: draft_source was stored "for the
    review and the pre-publish screen" and read by neither). None when the
    draft says nothing about its source."""
    ds = (saved_review or {}).get("draft_source") or {}
    if not ds:
        return None
    src = ds.get("source") or ds.get("chosen") or "model"
    solver = ds.get("solver") or {}
    critic = solver.get("critic") or {}
    code_dates = list(ds.get("code_dates") or [])
    words = _SOURCE_WORDS.get(src, str(src).replace("_", " "))
    text = f"Drafted by {words}"
    if code_dates and src == "model":
        text += f"; {len(code_dates)} {_plural(len(code_dates), 'day')} built in code"
    text += "."
    if critic.get("called"):
        k, rf = len(critic.get("kept") or []), len(critic.get("refused") or [])
        text += (f" The AI model reviewed it: {k} {_plural(k, 'change')} kept, {rf} refused by the rules.")
    return {"source": src, "label": words, "code_dates": code_dates, "text": text,
            "critic": ({"kept": [str(x.get("text") if isinstance(x, dict) else x) for x in critic.get("kept") or []][:6],
                        "refused": [str(x.get("text") if isinstance(x, dict) else x)
                                    for x in critic.get("refused") or []][:6]} if critic.get("called") else None),
            "conflicts": [str(x.get("text") if isinstance(x, dict) else x) for x in solver.get("conflicts") or []][:3],
            "proved_optimal": bool(solver.get("proved_optimal"))}


def _why_targets(rid, rows, c, econ, likely, limit) -> list:
    """The shifts worth explaining, by RISK not by the AI's own allocation
    (TRUST-12: the 3 days with the most SCHEDULED hours explained the same
    person three times): the closers of the sales-busiest days (the
    forecast's target hours for the day; scheduled hours only when no
    forecast was kept), the keyholders, people in training, then the rows
    the owner usually changes (the edit predictor) — one shift per person.
    [(index, why_this_one)]."""
    import schedule_time as _st
    by_day = {}
    for i, r in enumerate(rows):
        if (r.get("employee") or "").strip():
            by_day.setdefault(r.get("date"), []).append(i)
    demand = {}
    for d, h in ((econ or {}).get("daily_target_hours") or {}).items():
        try:
            demand[str(d)[:10]] = float(h or 0)
        except (TypeError, ValueError):
            continue
    if not demand:
        for d, reqs in ((econ or {}).get("requirements_by_date") or {}).items():
            try:
                demand[str(d)[:10]] = float(sum(float(x.get("people") or x.get("count") or 0)
                                                for x in (reqs or []) if isinstance(x, dict)))
            except (TypeError, ValueError, AttributeError):
                continue
    basis = "busiest" if demand else "most_hours"
    if not demand:
        demand = {d: sum(_hours(rows[i]) for i in idx) for d, idx in by_day.items()}
    picks, seen = [], set()

    def take(i, why):
        p = _low(rows[i].get("employee"))
        if p and p not in seen and len(picks) < limit:
            seen.add(p)
            picks.append((i, why))

    for d in sorted(by_day, key=lambda d: (-demand.get(d, 0), d)):
        idx = by_day[d]
        roles = {}
        for i in idx:
            roles[(rows[i].get("role") or "").strip()] = roles.get((rows[i].get("role") or "").strip(), 0) + 1
        role = max(roles.items(), key=lambda kv: (kv[1], kv[0]))[0] if roles else ""
        # The last of the day's biggest role out — not the one already
        # explained: the next closer down, so one person is said once.
        for i in sorted((i for i in idx if (rows[i].get("role") or "").strip() == role),
                        key=lambda j: (-(_st.end_of(rows[j]) or 0), rows[j].get("employee") or "")):
            if _low(rows[i].get("employee")) not in seen:
                take(i, "close" if basis == "busiest" else "close_of_most_hours")
                break
        if len(picks) >= max(2, limit - 2):
            break
    keys = set(getattr(c, "keyholders", None) or set()) if c is not None else set()
    trainees = set((getattr(c, "trainees", None) or {}).keys()) if c is not None else set()
    for i, r in enumerate(rows):
        k = c.key(r.get("employee")) if c is not None and hasattr(c, "key") else _low(r.get("employee"))
        if k in trainees:
            take(i, "training")
    for f in (likely or {}).get("rows") or []:
        for i, r in enumerate(rows):
            if _rkey(r.get("date"), r.get("shift_start"), r.get("employee")) == \
                    _rkey(f.get("date"), f.get("shift_start"), f.get("employee")):
                take(i, "usually_changed")
    for d in sorted(by_day, key=lambda d: (-demand.get(d, 0), d)):
        for i in sorted(by_day[d], key=lambda j: -(_st.end_of(rows[j]) or 0)):
            k = c.key(rows[i].get("employee")) if c is not None and hasattr(c, "key") else _low(rows[i].get("employee"))
            if k in keys:
                take(i, "keyholder")
                break
    return picks


_WHY_WORDS = {"close": "close", "close_of_most_hours": "close", "training": "in training", "keyholder": "closing keys",
              "usually_changed": "a shift you usually change"}


def weighed_assignments(rid, rows, c, scores, quality, limit=WHY_LIMIT, saved_review=None, likely=None,
                        econ=None) -> list:
    """Why these people, on the shifts that matter (TRUST-12): each chosen
    shift (_why_targets) with the ENGINE's own reason when the solve that
    drafted it recorded one and the row still carries its pick (`basis`
    "engine"), else a comparison with the best legal alternative made
    after the draft (weigh_one, `basis` "after") — labelled, never passed
    off as the engine's (TRUST-2). A comparison never argues against the
    pick (TRUST-1)."""
    if c is None or not rows:
        return []
    import schedule_engine as _se
    usual = _safe(lambda: __import__("models").get_prior_shift_pattern(rid), {}, "usual pattern", rid) or {}
    reasons = engine_reasons(saved_review)
    prepared = None
    memo = {}
    out = []
    for i, why in _why_targets(rid, rows, c, econ, likely, limit):
        r = rows[i]
        d = r.get("date")
        role = (r.get("role") or "").strip()
        where = f"{_weekday(d)[:3]} {_mdy(d)} {role} {_WHY_WORDS.get(why, '')}".strip() + ": "
        eng = reasons.get(_rkey(d, r.get("shift_start"), r.get("employee")))
        if eng:
            out.append({"employee": r.get("employee"), "runner_up": eng.get("runner_up"), "date": d,
                        "shift_start": r.get("shift_start"), "shift_id": r.get("shift_id") or eng.get("shift_id"),
                        "role": role, "why_this": why, "basis": "engine", "basis_label": "The solver's reason",
                        "favours_pick": True, "facts": eng.get("facts") or [], "swap": None,
                        "text": where + eng["text"]})
            continue
        if prepared is None:
            prepared = _se.prepare_replacements(rid, rows, constraints=c)
        got = weigh_one(rid, rows, i, c, scores, usual, prepared, memo=memo)
        if got:
            out.append(dict(got, date=d, shift_start=r.get("shift_start"), shift_id=r.get("shift_id"), role=role,
                            why_this=why, text=where + got["text"]))
    return out


def shift_why(rid, rows, index, c, scores, usual, runner=None, schedule_id=None) -> dict:
    """The Shift pane's "What Cavnar AI weighed" for rows[index] (the
    replacements route, both clients): the engine's own reason when the
    saved draft `schedule_id` recorded one for this row with this person
    (`basis` "engine", contract #4 / TRUST-2), else weigh_one against the
    best legal alternative (`basis` "after")."""
    if schedule_id and 0 <= index < len(rows):
        row = _safe(lambda: _row(rid, schedule_id), None, "shift why row", rid)
        if row:
            try:
                saved = json.loads(row.get("review_json") or "null") or {}
            except Exception:
                saved = {}
            r = rows[index]
            eng = engine_reasons(saved).get(_rkey(r.get("date"), r.get("shift_start"), r.get("employee")))
            if eng:
                return {"employee": r.get("employee"), "runner_up": eng.get("runner_up"), "basis": "engine",
                        "basis_label": "The solver's reason", "favours_pick": True, "swap": None,
                        "facts": eng.get("facts") or [], "text": eng["text"]}
    return weigh_one(rid, rows, index, c, scores, usual, runner=runner)


_SEARCH = object()


def weigh_one(rid, rows, index, c, scores, usual, prepared=None, runner=_SEARCH, memo=None) -> dict:
    """{employee, runner_up, text, basis, basis_label, favours_pick, swap} —
    rows[index]'s person against the best legal alternative (the
    replacements ranking), contrasted on rating, room in their week and
    whether they usually work that weekday — a comparison made AFTER the
    draft (`basis` "after"). None when nobody else could legally take it
    ("the only legal choice"). `runner` — the best legal alternative when
    the caller already ranked them (the replacements picker, which checked
    every one); None means nobody. `memo` — the review's roles, read once."""
    import schedule_engine as _se
    r = rows[index]
    name = (r.get("employee") or "").strip()
    if not name:
        return None
    role_low = (r.get("role") or "").strip().lower()
    if runner is _SEARCH:
        pool = _role_pool(rid, rows, role_low, _low(name), memo if memo is not None else {})
        pool.sort(key=lambda n: (-(scores.get(n) or 0), n))
        runner = None
        for n in pool[:REPLACEMENT_CHECKS]:
            ok, _w = _se.replacement_is_legal(rid, rows, index, n, constraints=c, prepared=prepared)
            if ok:
                runner = n
                break
    wd = _weekday(r.get("date"))

    def facts(n):
        have = sum(_hours(x) for x in rows if _low(x.get("employee")) == _low(n))
        cap = None
        try:
            cap = float(c.max_hours(n))
        except Exception:
            pass
        u = usual.get(n) or next((v for k, v in usual.items() if _low(k) == _low(n)), {}) or {}
        return {"rating": scores.get(n), "have": have, "room": (cap - have) if cap else None,
                "usual_day": wd in (u.get("days") or [])}
    me = facts(name)
    base = {"basis": "after", "basis_label": "Compared after the draft"}
    if runner is None:
        return dict(base, employee=name, runner_up=None, favours_pick=True, swap=None,
                    text=f"{name} — nobody else who works {r.get('role') or 'that role'} could legally take it.")
    rv = facts(runner)
    # Every bit is FOR the pick, each direction-checked (TRUST-1: the
    # rating contrast was appended whichever way it pointed, and printed
    # "Ben over Cara: Ben is rated 2/5 against Cara's 5/5").
    bits = []
    if me["rating"] and rv["rating"] and me["rating"] > rv["rating"]:
        bits.append(f"{name} is rated {me['rating']}/5 against {runner}'s {rv['rating']}/5")
    elif me["rating"] and not rv["rating"]:
        bits.append(f"{name} is rated {me['rating']}/5; {runner} isn't rated yet")
    if me["usual_day"] and not rv["usual_day"]:
        bits.append(f"{name} usually works {wd}s")
    if me["room"] is not None and rv["room"] is not None and me["room"] - rv["room"] >= 2:
        bits.append(f"{name} has {max(0, me['room']):g}h of room left against {runner}'s {max(0, rv['room']):g}h")
    stronger = bool(rv["rating"] and (not me["rating"] or rv["rating"] > me["rating"]))
    swap = {"label": f"Give it to {runner}", "action": "edit_rows",
            "payload": {"change": [{"match": _match(r), "set": {"employee": runner}}]}}
    if stronger:
        # The runner-up is rated higher: said as that — what kept the pick,
        # or that nothing did, with the one-tap swap.
        mine = f"{me['rating']}/5" if me["rating"] else "not rated yet"
        if bits:
            return dict(base, employee=name, runner_up=runner, favours_pick=False, swap=swap,
                        text=(f"{runner} is rated higher ({rv['rating']}/5 against {name}'s {mine}) and could take "
                              f"it; {name} was kept because " + "; ".join(bits) + "."))
        return dict(base, employee=name, runner_up=runner, favours_pick=False, swap=swap,
                    text=(f"{runner} could legally take it and is rated higher ({rv['rating']}/5 against {name}'s "
                          f"{mine}) — swap?"))
    if not bits:
        # Nothing on file favoured the pick: said so, never dressed up.
        more = (f", with more room in their week ({max(0, rv['room']):g}h against {max(0, me['room']):g}h)"
                if me["room"] is not None and rv["room"] is not None and rv["room"] - me["room"] >= 2 else "")
        return dict(base, employee=name, runner_up=runner, favours_pick=False, swap=None,
                    text=(f"{runner} could legally have taken it too{more}; nothing on file — rating, usual days, "
                          f"hours — made {name} the stronger pick."))
    return dict(base, employee=name, runner_up=runner, favours_pick=True, swap=None,
                text=f"{name} over {runner}: " + "; ".join(bits) + ".")


# ── 7. how sure we are ─────────────────────────────────────────────────────

def _empty_sure() -> dict:
    return {"kept_share": None, "kept_trend": None, "kept_weeks": 0, "predictor_hit": None, "predictor_base": None,
            "predictor_ready": False, "completeness": None, "forecast": None,
            "confidence": {"pct": None, "basis": [], "level": None}, "lines": []}


def _sure(rid, quality, likely, numbers) -> dict:
    """The track record (TRUST-18, TRUST-20): the share of each draft owners
    sent unchanged (schedule_versions.acceptance) with its trend, the edit
    predictor's hit rate against chance, the sales forecast's record, and
    the quality read's completeness — and a week-level confidence that is
    the plain average of whichever of those are MEASURED, each named as its
    basis; null when none is."""
    import schedule_versions as _sv
    out = _empty_sure()
    acc = _sv.acceptance(rid, weeks=KEPT_WEEKS)
    looked = [w for w in acc.get("weeks") or [] if w.get("unchanged_share") is not None]
    parts = []
    if looked:
        out["kept_share"] = acc.get("mean_unchanged_share")
        out["kept_weeks"] = len(looked)
        t = acc.get("trend")
        if t:
            out["kept_trend"] = {"direction": t["direction"], "older": t["older"], "newer": t["newer"]}
        share = int(round(100 * float(out["kept_share"])))
        line = f"Your last {len(looked)} {_plural(len(looked), 'draft')} went out with {share}% of shifts unchanged"
        if t:
            line += f" ({int(round(100 * t['older']))}% → {int(round(100 * t['newer']))}%, {t['direction']})"
        out["lines"].append(line + ".")
        parts.append((share, f"drafts kept unchanged: {share}% over {len(looked)} weeks"))
    if likely:
        out["predictor_hit"] = likely.get("hit_rate")
        out["predictor_base"] = likely.get("base_rate")
        out["predictor_ready"] = bool(likely.get("ready"))
        if likely.get("ready") and likely.get("hit_rate") is not None:
            base = likely.get("base_rate")
            out["lines"].append(f"The rows you usually change are marked (it guessed right "
                                f"{int(round(100 * likely['hit_rate']))}% of the time"
                                + (f", against {int(round(100 * base))}% by chance" if base is not None else "")
                                + ").")
    fc = None
    lp = (numbers or {}).get("labor_pct") or {}
    rec = _safe(lambda: forecast_record(rid), None, "forecast_record", rid)
    if rec:
        fc = {"within_pct": rec["within_pct"], "weeks_within": rec["weeks_within"], "weeks": rec["weeks"],
              "mean_error_pct": rec["mean_error_pct"], "skill": rec.get("skill"),
              "beats_last_week": rec.get("beats_last_week"), "vs_last_week": rec.get("vs_last_week")}
        hit = int(round(100.0 * rec["weeks_within"] / rec["weeks"]))
        if forecast_has_skill(rec):
            parts.append((max(0, 100 - int(round(rec["mean_error_pct"]))),
                          f"sales forecast off by {rec['mean_error_pct']:g}% on average over {rec['weeks']} weeks"))
        else:
            # A forecast no better than repeating last week earns no
            # confidence for its small misses (TRUST-30).
            parts.append((0, f"sales forecast no better than a naive guess over {rec.get('n_skill') or rec['weeks']} weeks"))
        out["lines"].append(lp.get("track_record") or forecast_skill_line(rec)
                            or f"Sales forecasts within {rec['within_pct']}% on {hit}% of weeks.")
    out["forecast"] = fc
    conf = (quality or {}).get("confidence") or {}
    if conf.get("score") is not None:
        out["completeness"] = int(conf["score"])
        top = conf.get("top_reason")
        out["lines"].append(f"Shift quality {quality.get('score')}/100, scored on {int(conf['score'])}% of what it needs"
                            + (f" ({top})" if top else "") + ".")
    # The week-level confidence (re-audit 10/11/26 TRUST-6): the K1
    # confidence engine over how this restaurant's PUBLISHED weeks RAN —
    # never the share of drafts sent unchanged (it rose when owners stopped
    # reading), never averaged with the forecast's error or the scorer's
    # input completeness. "—" below SURE_MIN_WEEKS measured weeks; the
    # engine's own caps hold it under 70 without a track record of
    # MIN_MEASURED weeks. The lines above stay as context, not as the %.
    out["confidence"] = week_confidence(rid)
    if out["confidence"].get("line"):
        out["lines"].insert(0, out["confidence"]["line"])
    return out


def weeks_ran(rid, weeks=SURE_WEEKS) -> list:
    """[{week_start, week_end, ok, failed: [test]}] — each published week
    the Schedule Learning Scorecard measured as it RAN (schedule_learning_
    scores), newest first, judged by the tests it has a measurement for
    (module constants: no minute without a manager, no uncovered no-show,
    staffing within RAN_SHORTFALL_HOURS of what was asked, labor within
    RAN_LABOR_OVER_PTS of target, overtime within RAN_OT_OVER_HOURS of the
    plan). A week with none of them measured is not counted."""
    import schedule_scorecard as _ssc
    out = []
    for r in _ssc._rows("restaurant_id=?", (rid,), limit=weeks):
        tests, failed = 0, []
        if r.get("unmanaged_minutes") is not None:
            tests += 1
            if float(r["unmanaged_minutes"]) > 0:
                failed.append("minutes with no manager")
        if r.get("uncovered_no_shows") is not None:
            tests += 1
            if int(r["uncovered_no_shows"]) > 0:
                failed.append("a no-show nobody covered")
        if r.get("shortfall_hours") is not None:
            tests += 1
            if float(r["shortfall_hours"]) > RAN_SHORTFALL_HOURS:
                failed.append("short of the staffing asked")
        if r.get("labor_vs_target") is not None:
            tests += 1
            if float(r["labor_vs_target"]) > RAN_LABOR_OVER_PTS:
                failed.append("labor over target")
        if r.get("ot_hours") is not None:
            tests += 1
            if float(r["ot_hours"]) > float(r.get("ot_hours_planned") or 0) + RAN_OT_OVER_HOURS:
                failed.append("more overtime than planned")
        if tests:
            out.append({"week_start": r.get("week_start"), "week_end": r.get("week_end"), "ok": not failed,
                        "failed": failed})
    return out


def week_confidence(rid, today=None) -> dict:
    """{pct, level, basis: [..], line, weeks_measured, weeks_ran_ok, k1} —
    the K1 object (confidence_engine.assemble) over weeks_ran: Evidence the
    measured weeks against a full sample ("weeks"), Historical Accuracy the
    90% Wilson lower bound of the weeks that ran as planned (only at the
    engine's MIN_MEASURED weeks; below it the no-track-record cap binds),
    Data Freshness the age of the newest measured week. pct None below
    SURE_MIN_WEEKS — "—" on screen, never a guess (TRUST-6)."""
    import confidence_engine as ce
    ran = _safe(lambda: weeks_ran(rid), [], "weeks ran", rid)
    n, ok = len(ran), sum(1 for w in ran if w["ok"])
    if n < SURE_MIN_WEEKS:
        return {"pct": None, "level": None, "weeks_measured": n, "weeks_ran_ok": ok, "k1": None, "line": None,
                "basis": [f"{n} of the {SURE_MIN_WEEKS} published weeks it needs have been measured as they ran"
                          if n else "no published week has been measured against how it ran yet"],
                "meaning": ce.MEANING}
    ev = ce.evidence(n=n, kind="weeks", basis=f"{n} published {_plural(n, 'week')} measured as they ran")
    if n >= ce.MIN_MEASURED:
        lo, _hi = ce.wilson(ok, n)
        lo_b, hi_b = ce.ACCURACY_BOUNDS
        acc = {"pct": int(max(lo_b, min(hi_b, round(100 * (lo or 0.0))))), "n": n, "improved": ok,
               "basis": f"ran as planned {ok} of {n} weeks", "source": "weeks_ran", "low": lo, "high": _hi,
               "lift": None, "prior": None}
    else:
        acc = {"pct": None, "n": n, "improved": ok, "basis": f"ran as planned {ok} of {n} weeks — too few to rate",
               "source": "weeks_ran", "low": None, "high": None, "lift": None, "prior": None}
    newest = max((str(w.get("week_end") or w.get("week_start") or "")[:10] for w in ran), default="")
    try:
        t = today or date.today()
        lag = (t - date.fromisoformat(newest)).days
    except (TypeError, ValueError):
        lag = None
    fr_pct = None if lag is None else int(round(100 * ce.recency(lag, 14, 42)))
    fr = {"pct": fr_pct, "basis": (f"the newest measured week ended {_mdy(newest)}" if newest else "undated"),
          "as_of": _mdy(newest) or None, "as_of_iso": newest or None, "stalest": "scorecard",
          "stalest_basis": None, "errors": []}
    k1 = ce.assemble(ev, acc, fr)
    pct = k1.get("pct")
    fails = {}
    for w in ran:
        for f in w["failed"]:
            fails[f] = fails.get(f, 0) + 1
    worst = max(fails.items(), key=lambda kv: kv[1])[0] if fails else None
    line = (f"{ok} of your last {n} published {_plural(n, 'week')} ran as planned"
            + (f" (most often missed: {worst})" if worst else "") + ".")
    return {"pct": pct, "level": ce.band(pct) if pct is not None else None, "weeks_measured": n, "weeks_ran_ok": ok,
            "k1": k1, "line": line, "meaning": ce.MEANING,
            "basis": [b for b in (ev.get("basis"), acc.get("basis"), fr.get("basis"), k1.get("reason")) if b]}


# ── 8. send ────────────────────────────────────────────────────────────────

def _empty_send() -> dict:
    return {"reach": None, "label": "Send to staff", "overriding": 0, "unsent_changes": None, "delivery": None,
            "one_tap_safe": False}


def _send(rid, row, rows, gate, payload, can_publish) -> dict:
    import people
    import schedule_versions as _sv
    names = sorted({(r.get("employee") or "").strip() for r in rows if (r.get("employee") or "").strip()})
    unsent = None
    if row.get("published_at"):
        unsent = (_sv.unsent_changes(rid, row["id"]) or {}).get("people") or []
        names = unsent
    s = people.reach_summary(people.reach(rid, names))
    reach = {"app": s.get("by_app", 0), "text": s.get("by_text", 0), "email": s.get("by_email", 0),
             "portal_only": len(s.get("unreachable") or []), "total": s.get("total", 0),
             "reachable": s.get("reachable", 0), "unreachable": s.get("unreachable") or []}
    over = len(payload.get("must_fix") or [])
    who = reach["reachable"]
    label = (f"Send to {who} staff" if who else "Publish to the staff portal")
    if row.get("published_at"):
        label = f"Send the changes to {len(unsent or [])} {_plural(len(unsent or []), 'person', 'people')}"
    delivery = None
    if row.get("published_at"):
        import schedule_publish as _sp
        delivery = _safe(lambda: _sp.notice_status(rid, row["id"]), None, "delivery", rid)
    review = {"blockers": gate.get("blockers") or [], "soft": gate.get("soft") or [], "notes": gate.get("notes") or [],
              "held_unattended": gate.get("held_unattended") or []}
    one_tap = bool(can_publish and not row.get("published_at") and not row.get("superseded_by")
                   and not (review["blockers"] or review["soft"] or review["notes"] or review["held_unattended"])
                   and reach["total"] > 0)
    return {"reach": reach, "label": label, "overriding": over, "unsent_changes": unsent, "delivery": delivery,
            "one_tap_safe": one_tap, "pos_push": _safe(lambda: pos_push_status(rid, row), None, "pos push", rid)}


def pos_push_status(rid, row):
    """How the sent week's write to the POS went (J2, RM-16:
    pos_schedule_push.last_push) — {status, failed, sent_on, text}, or None
    when the week was never pushed. A failure is said after Send, never
    only in the owner's inbox."""
    if not row.get("published_at"):
        return None
    import pos_schedule_push as _psp
    p = _psp.last_push(rid, row["week_start"])
    if not p:
        return None
    status = p.get("status")
    failed = sum(1 for s in p.get("shifts") or [] if s.get("status") in ("failed", "problem"))
    why = str(p.get("error") or p.get("reason") or "").strip()
    if status in ("failed", "refused"):
        text = "The week didn't reach your POS" + (f": {why}" if why else "") + "."
    elif status == "partial" or failed:
        text = f"{failed} {_plural(failed, 'shift')} didn't reach your POS" + (f": {why}" if why else "") + "."
    elif status in ("queued", "sending"):
        text = "Writing the week to your POS now."
    elif status in ("sent", "unchanged"):
        text = "In your POS" + (f" since {p['sent_on']}" if p.get("sent_on") else "") + "."
    else:
        text = None
    return {"status": status, "failed": failed, "sent_on": p.get("sent_on"), "text": text}


def weeks_holding(restaurant_id, name, today=None, db_path=None) -> list:
    """The weeks still to come that give `name` shifts: every unsent draft
    in force and every published week, each {history_id, week_start,
    week_mdy, published, shifts, dates} — said the moment someone is taken
    off the roster (owner, 10/14/26), never first at Publish."""
    import models as _m
    import schedule_shifts as _ss
    from staff_settings import name_key
    t = today or date.today()
    t_iso = t.isoformat() if hasattr(t, "isoformat") else str(t)[:10]
    key = name_key(name)
    conn = _m.get_conn(db_path) if db_path else _m.get_conn()
    try:
        weeks = conn.execute("SELECT id, week_start, week_end, published_at FROM schedule_history WHERE restaurant_id=? "
                             "AND superseded_by IS NULL AND substr(COALESCE(week_end, week_start),1,10) >= ? "
                             "ORDER BY week_start", (int(restaurant_id), t_iso)).fetchall()
    finally:
        conn.close()
    out = []
    for w in weeks:
        copy = "live" if w["published_at"] else "working"
        mine = [r for r in _ss.rows_of(w["id"], copy, db_path=db_path, restaurant_id=restaurant_id)
                if name_key(r.get("employee") or "") == key and str(r.get("date") or "") >= t_iso]
        if mine:
            out.append({"history_id": w["id"], "week_start": w["week_start"], "week_mdy": _mdy(w["week_start"]),
                        "published": bool(w["published_at"]), "shifts": len(mine),
                        "dates": sorted({str(r.get("date"))[:10] for r in mine})})
    return out

