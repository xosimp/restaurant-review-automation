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
"""
import json
import logging
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
REPLACEMENT_CHECKS = 15        # candidates tried per item, strongest first
CREW_DECIDE = 3                # Crew shifts leaner than usual offered as decisions
REGULAR_HOURS = 16             # "a regular": usually at least this many hours a week
CONFIDENCE_MIN_PARTS = 1       # measured parts before a week-level confidence is stated
# Soft kinds that are reminders, not decisions: one FYI line each (TRUST-22).
REMINDER_KINDS = ("meal_break", "shift_too_short")
# The edit and endpoint names a `post` action may name (API_REFERENCE).
POST_PATHS = ("labor/crew-matrix", "labor/standby/ask")


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

def review(restaurant_id, schedule_id, rows=None, user=None, today=None, can_publish=True) -> dict:
    """The contract #6 payload for `schedule_id` (see the module doc and
    API_REFERENCE). `rows` — the week as it stands on screen (unsaved
    edits), checked without saving; else the stored working copy. `user` —
    the reader (their labor basis, the override candidates). Never raises:
    a section that could not be worked out is empty."""
    import schedule_publish as _sp
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
        gate = {"blockers": [{"key": "check_failed", "tier": _sp.MUST_FIX, "rank": -1, "date": None,
                              "text": "The publish check couldn't run just now — try again before sending"}],
                "soft": [], "notes": [], "held_unattended": [], "hours": None}
    row, week_rows = keep["row"], keep.get("rows") or []
    c, viols = keep.get("constraints"), keep.get("violations") or []
    saved_review, quality = keep.get("review") or {}, keep.get("quality") or {}
    dates = _week_dates(row)
    scores = _safe(lambda: __import__("models").get_operational_scores(rid), {}, "scores", rid)
    out = {"ok": True, "schedule_id": row["id"], "week_start": row["week_start"], "week_end": row["week_end"],
           "published_at": row.get("published_at"), "version": _version(rid, row["id"]),
           "checked_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
           "rows_source": "screen" if rows is not None else "saved", "can_publish": bool(can_publish),
           "replaced_reason": _safe(lambda: __import__("schedule_versions").replaced_error(row), None, "replaced", rid)}
    crew = _safe(lambda: _crew(rid, row, rows), None, "crew", rid)
    out["must_fix"] = _safe(lambda: _must_fix(rid, gate, keep, user, scores), [], "must_fix", rid)
    left_off = _safe(lambda: left_off_reasons(rid, week_rows, c, scores, dates), [], "left_off", rid)
    out["decide"] = _safe(lambda: _decide(rid, gate, keep, crew, left_off), [], "decide", rid)
    out["fyi"] = _safe(lambda: _fyi(gate, viols), [], "fyi", rid)
    out["numbers"] = _safe(lambda: _numbers(rid, row, week_rows, c, dates), _empty_numbers(), "numbers", rid)
    out["quality"] = _safe(lambda: _quality(rid, row, quality), _empty_quality(), "quality", rid)
    out["changed"] = _safe(lambda: _changed(rid, row, week_rows, crew, saved_review, rows is not None),
                           {"vs_last_week": [], "vs_usual": [], "vs_last_draft": [], "cavnar_changes": []},
                           "changed", rid)
    out["narrative"] = _safe(lambda: _narrative(saved_review), [], "narrative", rid)
    out["fragile"] = _safe(lambda: _fragile(rid, week_rows, c, dates, quality), [], "fragile", rid)
    out["why"] = {"assignments": _safe(lambda: weighed_assignments(rid, week_rows, c, scores, quality), [],
                                       "why", rid),
                  "left_off": left_off}
    likely = None
    if not row.get("published_at"):
        likely = _safe(lambda: __import__("schedule_learning").likely_to_change(rid, week_rows), None, "likely", rid)
    out["likely_to_change"] = likely
    out["sure"] = _safe(lambda: _sure(rid, quality, likely, out["numbers"]), _empty_sure(), "sure", rid)
    out["send"] = _safe(lambda: _send(rid, row, week_rows, gate, out, can_publish), _empty_send(), "send", rid)
    must_n = len(out["must_fix"])
    holding = [d for d in out["decide"] if d.get("holds")]
    out["counts"] = {"must_fix": must_n, "decide": len(out["decide"]), "decide_holding": len(holding),
                     "fyi": len(out["fyi"]), "warnings": must_n + len(out["decide"])}
    out["verdict"] = verdict(out["must_fix"], out["decide"], row, week_rows, rows is not None)
    out["gate"] = {"blocker_keys": [b["key"] for b in gate.get("blockers") or []],
                   "always_overridden": gate.get("always_overridden") or []}
    return out


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
        r = conn.execute("SELECT id, week_start, week_end, schedule_csv, review_json, quality_json, "
                         "economics_json, published_at, superseded_by FROM schedule_history "
                         "WHERE id=? AND restaurant_id=?", (int(schedule_id), rid)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _stored_rows(row) -> list:
    from schedule_versions import rows_from_csv
    return rows_from_csv(row.get("schedule_csv") or "")


def _version(rid, hid):
    try:
        import schedule_versions as _sv
        v = _sv.newest_version(rid, hid)
        return v[0].get("version") if v else None
    except Exception:
        return None


# ── 0. the verdict ─────────────────────────────────────────────────────────

def verdict(must_fix, decide, row, rows, screen=False) -> dict:
    """The one verdict both clients lead with (TRUST-4, TRUST-6): red when
    anything must be fixed, amber when decisions are owed, green only when
    neither — never "The week is built ✓" over a broken week."""
    holding = [d for d in decide if d.get("holds")]
    if must_fix:
        first = must_fix[0]["text"].rstrip(".")
        more = len(must_fix) - 1
        tone, sentence = "red", f"Not ready: {first}" + (f", and {more} more to fix" if more else "") + "."
    elif holding:
        n = len(holding)
        tone, sentence = "amber", f"Ready after {n} {_plural(n, 'decision')}. Everything else checks out."
    else:
        tone, sentence = "green", "Ready to send. Nothing breaks your rules, and nothing needs a decision."
    people = {_low(r.get("employee")) for r in rows if (r.get("employee") or "").strip()}
    hrs = sum(_hours(r) for r in rows)
    sub = (f"Week of {_mdy(row['week_start'])} – {_mdy(row['week_end'] or row['week_start'])} · {len(rows)} "
           f"{_plural(len(rows), 'shift')} · {len(people)} {_plural(len(people), 'person', 'people')} · "
           f"{round(hrs):,}h · checked against today's time off and availability"
           + (" · your unsaved edits included" if screen else ""))
    return {"tone": tone, "sentence": sentence, "subline": sub}


# ── 1. must fix ────────────────────────────────────────────────────────────

def _must_fix(rid, gate, keep, user, scores) -> list:
    import schedule_publish as _sp
    import schedule_overrides as _so
    rows, c = keep.get("rows") or [], keep.get("constraints")
    breaches = keep.get("breaches") or {}
    prepared = None
    out = []
    for n, b in enumerate(b for b in gate.get("blockers") or [] if _sp.blocker_tier(b) == _sp.MUST_FIX):
        key, kind = b["key"], _so.kind_of(b["key"])
        v = breaches.get(key)
        needs = _sp.override_needs(key)
        item = {"key": key, "kind": kind, "tier": "must_fix", "rank": b.get("rank"), "text": b["text"],
                "date": b.get("date"), "day": _weekday(b.get("date")) if b.get("date") else None,
                "fixes": [], "override_allowed": True,
                "override": {"needs": needs, "candidates": [],
                             "prompt": ("Who runs the floor through it? You count."
                                        if needs == "covered_by" else "Why is it fine to send it anyway?")},
                "overridden_before": b.get("overridden_before")}
        if n < FIX_LOOKUPS and c is not None:
            try:
                if kind == "rule:no_manager" and v:
                    item["fixes"] = _manager_fixes(rows, c, v)
                    item["override"]["candidates"] = _floor_candidates(rows, c, v, user)
                elif kind.startswith("rule:") and v and not v.get("day_level") and v.get("index") is not None:
                    if prepared is None:
                        import schedule_engine as _se
                        prepared = _se.prepare_replacements(rid, rows, constraints=c)
                    item["fixes"] = _replacement_fixes(rid, rows, c, int(v["index"]), prepared, scores)
                elif kind in ("rule:coverage_floor", "rule:nobody_at_close", "rule:keyholder_until_close") and v:
                    item["fixes"] = _floor_fixes(rid, rows, c, v)
            except Exception as e:
                _capture(e, f"fix {kind}", rid)
        if kind == "roster:no_manager_roster":
            item["override"]["candidates"] = _floor_candidates(rows, c, None, user)
            item["fixes"].append({"label": "Name your managers", "action": "open",
                                  "payload": {"stage": "setup", "panel": "managers"}})
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


def _floor_candidates(rows, c, v, user) -> list:
    """Who could be named as running the floor through a manager gap: the
    people on during it, then the managers, then the reader (the owner
    counts — ARCH-30)."""
    import schedule_rules as _sr
    names = []
    if v:
        d = v.get("date")
        gs, ge = v.get("gap_start"), v.get("gap_end")
        for r in rows:
            if r.get("date") != d or not (r.get("employee") or "").strip():
                continue
            sp = _sr._span(r) if hasattr(_sr, "_span") else None
            if sp and gs is not None and ge is not None and not (sp[0] < int(ge) and int(gs) < sp[1]):
                continue
            if r["employee"] not in names:
                names.append(r["employee"])
    disp = getattr(c, "display", None) or {} if c is not None else {}
    for low in sorted((getattr(c, "managers", None) or {}) if c is not None else {}):
        nm = disp.get(low) or low.title()
        if nm not in names:
            names.append(nm)
    me = ((user or {}).get("name") or (user or {}).get("username") or "").strip() if isinstance(user, dict) else ""
    if me and me not in names:
        names.append(me)
    return names[:12]


def _replacement_fixes(rid, rows, c, index, prepared, scores, k=2) -> list:
    """Up to `k` people who could take rows[index] outright — legal by every
    rule the generation is checked against (schedule_engine.
    replacement_is_legal), strongest first — as one-tap edits."""
    import schedule_engine as _se
    import staff_settings as _ss
    if not (0 <= index < len(rows)):
        return []
    target = rows[index]
    role_low = (target.get("role") or "").strip().lower()
    current = _low(target.get("employee"))
    pool = []
    for e in _ss.roster(rid):
        name = (e.get("name") or "").strip()
        if not name or _low(name) == current:
            continue
        if role_low:
            known = {x for x in _ss.roles_for(rid, name)} | {(r.get("role") or "").strip().lower() for r in rows
                                                              if _low(r.get("employee")) == _low(name)}
            known.discard("")
            if known and role_low not in known:
                continue
        pool.append(name)
    pool.sort(key=lambda n: (-(scores.get(n) or 0), n))
    out = []
    for name in pool[:REPLACEMENT_CHECKS]:
        ok, _why = _se.replacement_is_legal(rid, rows, index, name, constraints=c, prepared=prepared)
        if not ok:
            continue
        have = sum(_hours(r) for r in rows if _low(r.get("employee")) == _low(name))
        rated = f", rated {scores[name]}/5" if scores.get(name) else ""
        out.append({"label": f"Give it to {name} (legal{rated}, {have + _hours(target):g}h this week)",
                    "action": "edit_rows",
                    "payload": {"change": [{"match": _match(target), "set": {"employee": name}}]}})
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
    "hours_over": [("Trim it — shows each cut", "week", "optimize"), ("Keep it this week", "week", "acknowledge")],
    "notice_short": [("Send it with less notice", "week", "acknowledge")],
    "late_change": [("Send the change", "week", "acknowledge")],
    "quality_unchecked": [("I've read the week — it can go", "week", "acknowledge")],
    "saved": [("I've read it", "week", "acknowledge")],
}


def _decide(rid, gate, keep, crew, left_off) -> list:
    import schedule_publish as _sp
    import schedule_overrides as _so
    out = []
    for b in gate.get("blockers") or []:
        if _sp.blocker_tier(b) != _sp.DECIDE:
            continue
        kind = _so.kind_of(b["key"])
        head = kind.split(":", 1)[0]
        opts = _DECIDE_OPTIONS.get(head) or [("Send it as it is", "week", "acknowledge")]
        options = [{"label": lab, "scope": scope, "action": act,
                    "payload": ({"key": b["key"]} if act == "acknowledge" else {})} for lab, scope, act in opts]
        text = b["text"]
        if head == "hours_over" and (b.get("over") or {}).get("dollars"):
            text += f" — about ${b['over']['dollars']:,} a week."
        out.append({"key": b["key"], "kind": kind, "text": text, "holds": True, "options": options,
                    "overridden_before": b.get("overridden_before")})
    # Crew: a shift leaner than the usual crew, unanswered (A's Crew cells,
    # people on at once) — the owner's answer is written where the engine
    # reads it (crew_answers_json / role_floors_json), the scope on the button.
    for cell in (_crew_under(crew) or [])[:CREW_DECIDE]:
        out.append(_crew_decision(rid, keep, crew, cell))
    # A regular left with no shift (D-37), with the first reason (TRUST-13).
    for p in left_off:
        if p.get("kind") in ("regular",):
            out.append({"key": f"left_off:{_low(p['name'])}", "kind": "left_off",
                        "text": f"{p['name']} usually works about {p.get('usual_hours'):g}h and has no shifts: "
                                f"{p['reason']}.",
                        "holds": False,
                        "options": [{"label": f"Find {p['name']} a shift", "scope": "week", "action": "open",
                                     "payload": {"stage": "schedule", "employee": p["name"]}},
                                    {"label": "Leave them off this week", "scope": "week", "action": "dismiss",
                                     "payload": {"key": f"left_off:{_low(p['name'])}"}}]})
    return [d for d in out if d]


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
    seen_people = {}
    for kind, vs in sorted(groups.items(), key=lambda kv: (kv[0] in REMINDER_KINDS, -len(kv[1]), kv[0])):
        # A breach named on every row of a person's week is one.
        people = {(_low(v.get("employee")), v.get("bucket") or v.get("date")) for v in vs}
        n = len(people) if kind in ("over_hours_ceiling", "under_min_hours", "daily_ot") else len(vs)
        seen_people[kind] = n
        label = _sr.LABELS.get(kind) or kind.replace("_", " ")
        if kind in REMINDER_KINDS:
            text = f"{n} {_plural(n, 'shift')}: {label}."
        elif n == 1:
            text = _sr.breach_text(vs[0])
        else:
            days = sorted({v.get("day") or _weekday(v.get("date")) for v in vs if v.get("date")})
            text = (f"{n} {_plural(n, 'shift')} {label}" + (f" ({', '.join(d[:3] for d in days[:4])}"
                                                           + ("…" if len(days) > 4 else "") + ")" if days else "")
                    + f" — e.g. {_sr.breach_text(vs[0])}")
        out.append({"key": f"soft:{kind}", "kind": kind, "text": text, "count": n})
    return out


# ── 3. numbers ─────────────────────────────────────────────────────────────

def _empty_numbers() -> dict:
    return {"labor_pct": None, "cost": None, "overtime_hours": None,
            "overtime": {"hours": None, "premium": None, "priced": False, "text": "Priced once the week can be priced"},
            "forecast_sales": None, "savings": None, "last_week": None}


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
    pricing = _econ.week_pricing(rid, c, blended_rate=econ.get("blended_rate") or None)
    if not pricing.get("blended_rate"):
        from models import get_restaurant
        r = get_restaurant(rid)
        pricing["blended_rate"] = getattr(r, "hourly_rate", None) or 15.0
    return econ, _econ.price_week(_sr.with_assumed_stays(rows, c), pricing)


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
    srt = sorted(errs)
    band = srt[min(len(srt) - 1, int(round(FORECAST_BAND * len(srt) + 0.4999)) - 1)]
    band = max(1.0, round(band))
    last = rows[0] if rows else None
    return {"weeks": len(errs), "within_pct": int(band), "weeks_within": sum(1 for e in errs if e <= band),
            "mean_error_pct": round(sum(errs) / len(errs), 1),
            "last": ({"projected": last.get("predicted"), "actual": last.get("actual"),
                      "period": last.get("horizon_end")} if last else None)}


def _numbers(rid, row, rows, c, dates) -> dict:
    import schedule_economics as _econ
    econ, cost = price_rows(rid, row, rows, c)
    out = _empty_numbers()
    rev = econ.get("projected_revenue")
    if rev:
        out["forecast_sales"] = {"value": round(float(rev)),
                                 "basis": econ.get("projected_revenue_source") or "the forecast this week was built on"}
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
                              labor_budget_dollars=econ.get("labor_budget_dollars"), week_dates=dates)
    rec = _safe(lambda: forecast_record(rid), None, "forecast_record", rid)
    if lv:
        pct = lv.get("pct")
        low = high = None
        track = None
        if rec and pct and rev:
            e = rec["within_pct"] / 100.0
            low, high = round(lv["cost"] / (float(rev) * (1 + e)) * 100, 1), \
                (round(lv["cost"] / (float(rev) * (1 - e)) * 100, 1) if e < 1 else None)
            track = (f"Our sales forecasts here were within {rec['within_pct']}% on {rec['weeks_within']} of the last "
                     f"{rec['weeks']} weeks.")
        out["labor_pct"] = {"value": pct, "low": low, "high": high, "basis": lv.get("basis"), "label": lv.get("label"),
                            "target": lv.get("target_pct"), "target_basis": lv.get("target_basis"),
                            "grade": lv.get("grade") or _econ.labor_grade(pct, lv.get("target_pct")),
                            "track_record": track, "text": lv.get("text"),
                            "other_basis_note": lv.get("other_basis_note")}
        out["cost"] = {"hourly": lv.get("hourly_cost"), "salaried": lv.get("salaried_cost"), "total": lv.get("cost"),
                       "text": (f"${lv.get('hourly_cost'):,.0f} hourly"
                                + (f" + ${lv['salaried_cost']:,.0f} salaried" if lv.get("salaried_cost") else ""))}
        if lv.get("savings") is not None:
            out["savings"] = {"value": lv["savings"], "projected": True, "recent_pct": lv.get("recent_pct"),
                              "recent_days": lv.get("recent_days"),
                              "basis": (f"against your last {lv.get('recent_days') or 14} days at {lv.get('recent_pct')}%"
                                        " — a projection, earned only if the week runs as drafted")}
    elif cost:
        out["cost"] = {"hourly": cost.get("total"), "salaried": None, "total": cost.get("total"),
                       "text": f"${cost.get('total') or 0:,.0f} hourly"}
    if rec and rec.get("last") and rec["last"].get("projected") and rec["last"].get("actual"):
        p, a = float(rec["last"]["projected"]), float(rec["last"]["actual"])
        diff = (a - p) / p * 100 if p else 0
        out["last_week"] = {"projected": round(p), "actual": round(a),
                            "text": (f"The week ending {_mdy(rec['last']['period'])} we projected ${p:,.0f} in sales; "
                                     f"you ran ${a:,.0f} ({'+' if diff >= 0 else '−'}{abs(diff):.0f}%).")}
    return out


def _labor_target(rid):
    from models import get_restaurant
    r = get_restaurant(rid)
    return getattr(r, "labor_target_pct", None) if r is not None else None


# ── the quality score, graded against the restaurant's own weeks ───────────

def _empty_quality() -> dict:
    return {"score": None, "band": None, "grade": "ungraded", "percentile": None, "weeks_compared": 0,
            "lead": None, "text": None}


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
        import schedule_learning as _sl
        for d in _sl.standby_days(rid, dates, rows=rows, limit=2):
            pct = int(round(100 * float(d["chance_of_a_no_show"])))
            sb = d.get("standby") or {}
            text = (f"{d['day'][:3]} {_mdy(d['date'])}: about a {pct}% chance someone doesn't show (from each person's "
                    f"record; the overall no-show rate here is {100 * float(d.get('base_rate') or 0):.1f}%).")
            action = None
            if sb.get("employee"):
                text += f" {sb['employee']} is off and could be on call."
                action = {"label": f"Ask {sb['employee']}", "action": "post",
                          "payload": {"path": "labor/standby/ask",
                                      "body": {"date": d["date"], "employee": sb["employee"],
                                               "shift_start": sb.get("shift_start"), "shift_end": sb.get("shift_end")}}}
            out.append({"key": f"standby:{d['date']}", "kind": "no_show", "text": text, "rank": 1 - pct / 100.0,
                        "action": action})
    except Exception as e:
        _capture(e, "standby", rid)
    if c is not None:
        try:
            for s in _sr.assumed_stays(rows, c):
                act = s.get("action") or {}
                p = act.get("payload") or {}
                match = {"shift_id": p.get("shift_id"), "employee": p.get("employee") or s.get("employee"),
                         "date": p.get("date") or s.get("date"), "shift_start": p.get("shift_start")}
                until = p.get("shift_end") or p.get("until") or s.get("until")
                out.append({"key": f"stay:{s.get('date')}:{_low(s.get('employee'))}", "kind": "assumed_stay",
                            "text": s.get("text"), "rank": 0.5,
                            "action": ({"label": act.get("label") or f"Schedule {s.get('employee')} to {until}",
                                        "action": "edit_rows",
                                        "payload": {"change": [{"match": match, "set": {"shift_end": until}}]}}
                                       if until else None)})
        except Exception as e:
            _capture(e, "assumed stays", rid)
        try:
            import schedule_learning as _sl2
            for f in _sl2.overtime_forecast(rows, c)[:2]:
                cand = f.get("candidate") or {}
                act = None
                if cand.get("employee") and cand.get("index") is not None and cand["index"] < len(rows):
                    act = {"label": f"Move a shift to {cand['employee']}", "action": "edit_rows",
                           "payload": {"change": [{"match": _match(rows[cand["index"]]),
                                                   "set": {"employee": cand["employee"]}}]}}
                out.append({"key": f"ot:{_low(f.get('employee'))}", "kind": "overtime", "text": f.get("text"),
                            "rank": 0.6, "action": act})
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

def left_off_reasons(rid, rows, c, scores=None, dates=None) -> list:
    """Everyone active on the roster with no shift this week, with the FIRST
    reason that kept them off (TRUST-13: the review listed bare names, so a
    GM assumed the AI forgot them): time off all week, unavailable on every
    open day, at their hours cap already, no shift in weeks, else the role's
    shifts were full with people rated higher. {name, reason, kind,
    usual_hours}."""
    if c is None:
        return []
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
        out.append({"name": name, "reason": reason, "kind": "regular" if (uh or 0) >= REGULAR_HOURS and
                    kind not in ("time_off", "dormant", "unavailable") else kind,
                    "cause": kind, "usual_hours": round(float(uh), 1) if uh else None})
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


def weighed_assignments(rid, rows, c, scores, quality, limit=3) -> list:
    """What Cavnar AI weighed on the assignments that mattered most (TRUST-14):
    the closing shift of the busiest days, each with the legal runner-up and
    the difference — rating, room left in their week, whether they usually
    work that day. Never a description written after the fact."""
    if c is None or not rows:
        return []
    import schedule_engine as _se
    usual = _safe(lambda: __import__("models").get_prior_shift_pattern(rid), {}, "usual pattern", rid) or {}
    by_day = {}
    for i, r in enumerate(rows):
        if (r.get("employee") or "").strip():
            by_day.setdefault(r.get("date"), []).append(i)
    hours_by_day = {d: sum(_hours(rows[i]) for i in idx) for d, idx in by_day.items()}
    busiest = sorted(by_day, key=lambda d: -hours_by_day.get(d, 0))[:limit]
    prepared = _se.prepare_replacements(rid, rows, constraints=c)
    out = []
    for d in busiest:
        idx = by_day[d]
        # the last person out of the day's biggest role: the closer
        roles = {}
        for i in idx:
            roles[(rows[i].get("role") or "").strip()] = roles.get((rows[i].get("role") or "").strip(), 0) + 1
        role = max(roles.items(), key=lambda kv: (kv[1], kv[0]))[0] if roles else ""
        cands = [i for i in idx if (rows[i].get("role") or "").strip() == role]
        import schedule_time as _st
        i = max(cands, key=lambda j: (_st.end_of(rows[j]) or 0, rows[j].get("employee") or ""))
        got = weigh_one(rid, rows, i, c, scores, usual, prepared)
        if got:
            out.append(dict(got, date=d, shift_start=rows[i].get("shift_start"), role=role,
                            text=f"{_weekday(d)[:3]} {_mdy(d)} {role} close: " + got["text"]))
    return out


_SEARCH = object()


def weigh_one(rid, rows, index, c, scores, usual, prepared=None, runner=_SEARCH) -> dict:
    """{employee, runner_up, text} — rows[index]'s person against the best
    legal alternative (the replacements ranking), contrasted on rating, room
    in their week and whether they usually work that weekday. None when
    nobody else could legally take it ("the only legal choice"). `runner`
    — the best legal alternative when the caller already ranked them (the
    replacements picker, which checked every one); None means nobody."""
    import schedule_engine as _se
    import staff_settings as _ss
    r = rows[index]
    name = (r.get("employee") or "").strip()
    if not name:
        return None
    role_low = (r.get("role") or "").strip().lower()
    if runner is _SEARCH:
        pool = []
        for e in _ss.roster(rid):
            n = (e.get("name") or "").strip()
            if not n or _low(n) == _low(name):
                continue
            known = set(_ss.roles_for(rid, n)) | {(x.get("role") or "").strip().lower() for x in rows
                                                  if _low(x.get("employee")) == _low(n)}
            known.discard("")
            if role_low and known and role_low not in known:
                continue
            pool.append(n)
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
    if runner is None:
        return {"employee": name, "runner_up": None,
                "text": f"{name} — nobody else who works {r.get('role') or 'that role'} could legally take it."}
    rv = facts(runner)
    bits = []
    if me["rating"] and rv["rating"] and me["rating"] != rv["rating"]:
        bits.append(f"{name} is rated {me['rating']}/5 against {runner}'s {rv['rating']}/5")
    elif me["rating"] and not rv["rating"]:
        bits.append(f"{name} is rated {me['rating']}/5; {runner} isn't rated yet")
    if me["usual_day"] and not rv["usual_day"]:
        bits.append(f"{name} usually works {wd}s")
    if me["room"] is not None and rv["room"] is not None and me["room"] - rv["room"] >= 2:
        bits.append(f"{name} has {max(0, me['room']):g}h of room left against {runner}'s {max(0, rv['room']):g}h")
    if not bits:
        # Nothing on file favoured the pick: said so, never dressed up.
        more = (f", with more room in their week ({max(0, rv['room']):g}h against {max(0, me['room']):g}h)"
                if me["room"] is not None and rv["room"] is not None and rv["room"] - me["room"] >= 2 else "")
        return {"employee": name, "runner_up": runner,
                "text": (f"{runner} could legally have taken it too{more}; nothing on file — rating, usual days, "
                         f"hours — made {name} the stronger pick.")}
    return {"employee": name, "runner_up": runner, "text": f"{name} over {runner}: " + "; ".join(bits) + "."}


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
              "mean_error_pct": rec["mean_error_pct"]}
        hit = int(round(100.0 * rec["weeks_within"] / rec["weeks"]))
        parts.append((max(0, 100 - int(round(rec["mean_error_pct"]))),
                      f"sales forecast off by {rec['mean_error_pct']:g}% on average over {rec['weeks']} weeks"))
        out["lines"].append(lp.get("track_record") or f"Sales forecasts within {rec['within_pct']}% on {hit}% of weeks.")
    out["forecast"] = fc
    conf = (quality or {}).get("confidence") or {}
    if conf.get("score") is not None:
        out["completeness"] = int(conf["score"])
        top = conf.get("top_reason")
        out["lines"].append(f"Shift quality {quality.get('score')}/100, scored on {int(conf['score'])}% of what it needs"
                            + (f" ({top})" if top else "") + ".")
        parts.append((int(conf["score"]), f"quality inputs complete: {int(conf['score'])}%"))
    if len(parts) >= CONFIDENCE_MIN_PARTS and (looked or rec):
        pct = int(round(sum(p for p, _b in parts) / len(parts)))
        out["confidence"] = {"pct": pct, "basis": [b for _p, b in parts],
                             "level": "high" if pct >= 85 else ("medium" if pct >= 65 else "low")}
    else:
        out["confidence"] = {"pct": None, "basis": [b for _p, b in parts] or
                             ["no published week has been measured against how it ran yet"], "level": None}
    return out


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
            "one_tap_safe": one_tap}
