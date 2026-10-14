"""
schedule_dedicated.py — a position the restaurant staffs on every covered
shift no matter what the forecast says: one extra person in a role, on given
dayparts and days, placed by code before the model writes the week.

Simple EJ's (Anthony, 10/9/26): every night and on Saturday and Sunday
daytime one more bartender is on, because one of them takes the bar's
numbered tables (201–205). Non-negotiable unless a manager writes an
allowance in the Studio's notes ("no bar tables bartender Tuesday").

How it is held, the way the managers' shifts are (schedule_skeleton):

  * restaurants.dedicated_shifts_json lists the positions —
    [{"label": "Bar tables 201–205", "family": "bartender",
      "parts": {"night": [every day], "morning": ["Saturday", "Sunday"]},
      "waive_words": ["bar table"]}];
  * plan() places one row per covered date and daypart: the role this
    restaurant writes that daypart's shifts of the family in (its history),
    at its usual times for that weekday, given to a person who holds the
    role — the family itself, never a name that merely contains it ("Server
    Assistant" is not a server, BUGSGEN-24) — and may legally take it
    (Constraints.fillable / can_add): last week's person in that slot
    first, then whoever usually works it, then the fewest hours in the
    payroll week (the managers' plan and every row already fixed counted),
    so the same bartender keeps bar tables week to week (LOGIC-17); never
    into overtime while somebody else can take it without, and past the
    restaurant's own limits only when nobody can inside them (FILL_ROUNDS);
  * the position is one of the role's usual crew when the restaurant has
    been running it — its punches already count that person — so the
    shift's requirement is the usual crew, at least the position; only a
    position newer than the history (the rule's "since") is one MORE on
    top of it (LOGIC-16: EJ's three usual Friday bartenders already include
    bar tables; a +1 on top asked for a fourth the bar never runs);
  * the rows carry "_pinned": DEDICATED_SOURCE and merge into the model's
    answer by code (labor.generate_optimized_schedule, restore_pinned), so
    no trim, fill or redo pass removes, re-times or re-assigns one;
  * a covered slot nobody can legally take is said in the review, with why;
  * a note sentence that LETS THE POSITION OFF — an allowance built on the
    position itself ("no bar tables bartender Tuesday", "skip the bar
    tables Monday", "bar tables not needed Sunday") — reads as a waiver of
    the slots it names (its days and dayparts; none named = the whole
    week). A sentence that insists ("never", "always", "keep", "must", "no
    matter what", "somebody else must take…") is never one, and a negation
    anywhere else in the sentence is not an allowance (blind re-audit
    10/11/26 BUGSGEN-4: "Bar tables are never off — always staff them"
    waived all nine slots). A waiver holds only once the owner CONFIRMED it
    (schedule_note_rules.add_waiver — the AI tab, or the Generate sheet for
    a draft's own instruction); until then the position is staffed and the
    review says the note was read but not applied.

The row's note is "<label> — Cavnar AI: standing position": the staff read
the label (labor.staff_facing_note strips the Cavnar AI mark).
"""
import json
import re

import schedule_time as _stime

DEDICATED_SOURCE = "dedicated"
NOTE_MARK = "Cavnar AI: standing position"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
PARTS = ("morning", "night")
PART_WORDS = {"morning": "daytime", "night": "night"}
# A position started (its "since") at least this long before the week is in
# the usual crew the requirements are read from: the baseline's recency
# weighting gives the last four weeks most of its weight.
IN_USUAL_DAYS = 28

_ALLOW_RX = re.compile(r"\b(no|skip|skipping|without|waive|waived|don'?t need|do not need|not needed|no need|"
                       r"drop|leave off|off|optional)\b", re.I)
# Words that insist on the position, or on somebody taking it — never an
# allowance, whatever negation sits beside them (BUGSGEN-4).
_KEEP_RX = re.compile(r"\b(never|always|keep|keeps|kept|must|make sure|ensure|require|requires|"
                      r"required|no matter|regardless|non-?negotiable|still|staff them|staff it|cover|covers|"
                      r"covered|somebody else|someone else|anyone else|take the|takes the|instead)\b", re.I)
# The allowance itself: a verb of letting off whose object is the position,
# or the position as the subject of "not needed" / "off" / "optional".
_LEAD = (r"(?:no|skip|skipping|without|waive|waived|waiving|drop|dropping|leave off|no need for|don'?t need|"
         r"do not need|cut)\s+(?:the\s+|a\s+|an\s+|any\s+|our\s+)?")
_TRAIL = (r"(?:\s+(?:bartender|server|position|person|shift|station|section)s?)?(?:\s+\w+){0,2}?\s+"
          r"(?:is\s+|are\s+|can\s+be\s+)?(?:not needed|isn'?t needed|aren'?t needed|off|waived|skipped|optional)\b")


def rules_of(restaurant) -> list:
    """The restaurant's positions, cleaned: each {"label", "family",
    "parts": {daypart: [days]}, "waive_words"}. [] when none or unreadable."""
    raw = getattr(restaurant, "dedicated_shifts_json", None) if restaurant is not None else None
    try:
        data = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    out = []
    for x in data if isinstance(data, list) else []:
        if not isinstance(x, dict) or not str(x.get("family") or "").strip():
            continue
        parts = {}
        for p, days in (x.get("parts") or {}).items():
            if p in PARTS:
                ds = [d for d in (days or []) if d in DAYS]
                if ds:
                    parts[p] = ds
        if not parts:
            continue
        label = str(x.get("label") or x["family"]).strip()[:60]
        words = [str(w).strip().lower() for w in (x.get("waive_words") or []) if str(w).strip()]
        since = str(x.get("since") or "").strip()[:10] or None
        out.append({"label": label, "family": str(x["family"]).strip().lower(), "parts": parts,
                    "waive_words": words or [label.lower()], "since": since})
    return out


def in_usual(rule, week_start) -> bool:
    """Whether the restaurant's history already runs the position on the
    week starting `week_start` (ISO): no "since" on the rule — a position
    the restaurant has been staffing (Simple EJ's: "every night one more
    bartender is on") — or one started IN_USUAL_DAYS or more before the
    week. Then the usual crew counts it and it is one of them; otherwise it
    is one more on top (LOGIC-16)."""
    since = (rule or {}).get("since")
    if not since:
        return True
    try:
        from datetime import datetime, timedelta
        start = datetime.strptime(str(week_start)[:10], "%Y-%m-%d")
        return datetime.strptime(since, "%Y-%m-%d") <= start - timedelta(days=IN_USUAL_DAYS)
    except (TypeError, ValueError):
        return True


def describe(rule) -> str:
    """"Bar tables 201–205: one more bartender every night and Saturday and
    Sunday daytime" — the owner's sentence for the position."""
    bits = []
    for p in PARTS:
        days = rule["parts"].get(p) or []
        if not days:
            continue
        when = "every " + PART_WORDS[p] if len(days) == 7 else (" and ".join(days) + " " + PART_WORDS[p])
        bits.append(when)
    return f"{rule['label']}: one more {rule['family']} " + " and ".join(bits)


def allows(low, words) -> bool:
    """Whether the sentence `low` (lower case) lets the position off: an
    allowance construction about the position itself (one of its waive
    `words`), and no word that insists on it (BUGSGEN-4)."""
    if not _ALLOW_RX.search(low) or _KEEP_RX.search(low):
        return False
    for w in words or ():
        pos = re.escape(str(w).lower()) + r"(?:s|es)?"
        if re.search(r"\b" + _LEAD + pos + r"\b", low) or re.search(r"\b" + pos + _TRAIL, low):
            return True
    return False


def waivers(text, rules) -> list:
    """[{"rule", "day", "part", "note"}]: the slots the notes' sentences
    read as waiving (allows); their days and dayparts narrow it (none =
    all). A READING: plan() is handed only the confirmed ones."""
    if not text or not rules:
        return []
    from schedule_note_rules import _DAYPART_TOKENS, _days, _normalise, _sentences
    out = []
    for s in _sentences(str(text)):
        low = " ".join(s.lower().replace("’", "'").split())
        if not _ALLOW_RX.search(low) or _KEEP_RX.search(low):
            continue
        tokens = re.findall(r"[a-z]+|\d+|-", _normalise(s))
        try:
            named, _why = _days(tokens)
        except Exception:
            named = None
        days = list(named or DAYS)
        parts = sorted({_DAYPART_TOKENS[t] for t in tokens if t in _DAYPART_TOKENS}) or list(PARTS)
        for i, r in enumerate(rules):
            if not allows(low, r["waive_words"]):
                continue
            for d in days:
                for p in parts:
                    if d in (r["parts"].get(p) or []):
                        out.append({"rule": i, "day": d, "part": p, "note": s.strip()[:200]})
    return out


def confirmed_waiver_keys(restaurant_id, week_dates=None, db_path=None) -> set:
    """The sentences (schedule_note_rules._key) whose waiver the owner
    confirmed and that are in force for the week: every standing one, and a
    one-week one for exactly this week."""
    import schedule_note_rules as _snr
    week = [str(d)[:10] for d in (week_dates or []) if d]
    out = set()
    try:
        sd = _snr._start_day(restaurant_id)
        mondays = {_snr._monday(d, sd).isoformat() for d in week}
        for r in _snr.note_rules(restaurant_id, week_start=(week or [None])[0], db_path=db_path or _snr.DB_PATH):
            if r.get("kind") != "waiver":
                continue
            if r["scope"] == "week" and mondays != {r.get("week_start")}:
                continue
            for src in [r.get("source_text")] + list(r.get("more_sources") or []):
                if src:
                    out.add(_snr._key(src))
    except Exception as e:
        print(f"[schedule_dedicated] confirmed waivers unread: {e!r}")
    return out


def split_waivers(text, rules, confirmed) -> tuple:
    """(waived, unconfirmed): the slots the confirmed sentences waive, and
    [{"note", "label"}] for each sentence read as a waiver the owner has not
    confirmed — the position stays staffed and the review says so."""
    from schedule_note_rules import _key, _sentences
    yes, no = [], []
    for s in _sentences(str(text or "")):
        (yes if _key(s) in (confirmed or set()) else no).append(s)
    waived = waivers("\n".join(yes), rules)
    pending = []
    for s in no:
        ws = waivers(s, rules)
        if ws:
            pending.append({"note": s.strip()[:200], "label": rules[ws[0]["rule"]]["label"]})
    return waived, pending


def _part_of(row):
    """The daypart a past row is, by the one rule (schedule_time.
    primary_daypart, BUGSGEN-25) — None when its times cannot be read."""
    part = _stime.primary_daypart(row)
    return part if part in PARTS else None


def _family_of(c, role) -> str:
    """The role family `role` belongs to — the restaurant's own map
    (Constraints.family), else the name with its daypart words taken off."""
    if c is not None and hasattr(c, "family"):
        return c.family(role)
    from shift_quality import role_family
    return role_family(role)


def _usual(history, family, day, part, c=None):
    """(role, start_min, end_min) this restaurant writes the family's
    `part` shifts in on `day` — the most common role, the median start and
    end — falling back to every weekday of that daypart. None with no
    history of the family in that daypart. A row is the family's by its
    role's family, never by the name containing it (BUGSGEN-24: "server"
    matched "Server Assistant", "cook" matched "Prep Cook")."""

    def pick(rows):
        if not rows:
            return None
        roles = {}
        for r in rows:
            roles[r.get("role") or ""] = roles.get(r.get("role") or "", 0) + 1
        role = max(sorted(roles), key=lambda k: roles[k])
        spans = [_stime.bounded_span(r) for r in rows if (r.get("role") or "") == role]
        spans = [s for s in spans if s]
        if not spans or not role:
            return None
        # Clock-in medians (3:56pm) written as a schedule writes them: the
        # nearest quarter hour.
        q = lambda m: int(round(float(m) / 15.0)) * 15  # noqa: E731
        return role, q(_stime.median([s[0] for s in spans])), q(_stime.median([s[1] for s in spans]))

    mine = [r for r in history or [] if _family_of(c, r.get("role")) == family and _part_of(r) == part]
    same_day = [r for r in mine if _stime.weekday_of(r.get("date")) == day]
    return pick(same_day) or pick(mine)


def plan(c, week_dates, rules, history=None, roster_roles=None, prior_rows=None, waived=None) -> dict:
    """{"rows", "unfilled", "waived", "rules"}: one pinned row per covered
    date and daypart (see the module note). `prior_rows` are rows already
    fixed (the managers' plan, a redo's kept days); `roster_roles` the
    {name: role} roster. Never raises: a failure is one unfilled entry."""
    import schedule_rules as _rules
    out = {"rows": [], "unfilled": [], "waived": [], "rules": [describe(r) for r in rules or []]}
    if not rules or not week_dates:
        return out
    waived = waived or []
    fixed = [dict(r) for r in (prior_rows or [])]
    names = sorted({n for n in (roster_roles or {}) if n})
    week_start = min(week_dates)

    def week_hours(n, d):
        """Hours `n` has in `d`'s payroll week: published or worked already
        (base_hours), every row fixed so far (the managers' plan, kept days)
        and this plan's rows — never only this plan's (LOGIC-17)."""
        k = c.key(n)
        b = c.bucket(d)
        h = float(((getattr(c, "base_hours", None) or {}).get(k) or {}).get(b, 0.0) or 0.0)
        return h + sum(_rules.row_hours(r) for r in fixed + out["rows"]
                       if c.key(r.get("employee")) == k and r.get("date") and c.bucket(r["date"]) == b)

    for i, rule in enumerate(rules):
        fam = rule["family"]
        # Who holds the role: its family, by the restaurant's own families —
        # never a role whose name merely contains it (BUGSGEN-24).
        people = [n for n in names
                  if _family_of(c, (roster_roles or {}).get(n)) == fam
                  or any(_family_of(c, r) == fam for r in (getattr(c, "known_roles", {}) or {}).get(c.key(n), ()))]
        keep = in_usual(rule, week_start)
        slot_people = _slot_people(history, fam, rule, c)
        for d in week_dates:
            day = _stime.weekday_of(d)
            for part in PARTS:
                if day not in (rule["parts"].get(part) or []):
                    continue
                if d in (getattr(c, "closed_dates", None) or ()):
                    continue
                w = next((x for x in waived if x["rule"] == i and x["day"] == day and x["part"] == part), None)
                if w:
                    out["waived"].append({"date": d, "part": part, "label": rule["label"], "note": w["note"]})
                    continue
                usual = _usual(history, fam, day, part, c)
                if not usual:
                    out["unfilled"].append({"date": d, "part": part, "label": rule["label"],
                                            "why": f"no {fam} {PART_WORDS[part]} shift on file to copy the times from"})
                    continue
                role, s, e = usual
                row = {"date": d, "day": day, "role": role, "shift_start": _stime.fmt_minutes(s),
                       "shift_end": _stime.fmt_minutes(e % 1440),
                       "notes": f"{rule['label']} — {NOTE_MARK}", "_pinned": DEDICATED_SOURCE,
                       "_pin_reason": rule["label"], "_in_usual": keep}
                # As every row carries it — a string, from the real span in
                # the restaurant's zone (BUGSGEN-6): it was a float of the
                # wall-clock span.
                row["scheduled_hours"] = _stime.row_hours_text(row, getattr(c, "tz", None))
                chosen, why_not = None, []
                last, usual_n = slot_people.get((day, part), (None, {}))
                # Last week's person in this slot, then whoever usually works
                # it, then the fewest hours in the payroll week (LOGIC-17).
                order = sorted(people, key=lambda n: (0 if last and c.key(n) == last else 1,
                                                      -usual_n.get(c.key(n), 0), week_hours(n, d), n))
                for mode in _rules.FILL_ROUNDS:
                    for n in order:
                        cand = dict(row, employee=n)
                        if any((r.get("employee") or "").strip().lower() == n.lower() and r.get("date") == d
                               and _stime.overlap_minutes(_stime.bounded_span(r) or (0, 0), (s, e)) > 0
                               for r in fixed + out["rows"]):
                            continue
                        ok, why = c.fillable(n, d)
                        if ok:
                            ok, why = c.can_add(cand, fixed + out["rows"], overtime=mode[0], policy=mode[1])
                        if ok:
                            chosen = cand
                            if mode != _rules.FILL_ROUNDS[0]:
                                chosen["_pin_reason"] = rule["label"] + (
                                    " (overtime: nobody else in the role could take it)" if mode[1] else
                                    " (past your scheduling limits: nobody else in the role could take it)")
                            break
                        if mode == _rules.FILL_ROUNDS[0] and why:
                            why_not.append(f"{n}: {why}")
                    if chosen:
                        break
                if not chosen:
                    out["unfilled"].append({"date": d, "part": part, "label": rule["label"],
                                            "why": "; ".join(why_not[:4]) or f"nobody holds a {fam} role"})
                    continue
                out["rows"].append(chosen)
    return out


def _slot_people(history, family, rule, c) -> dict:
    """{(weekday, daypart): (last person key or None, {person key: times})}
    from the history: who took the position in that slot most recently (a
    row marked as the position), and who works the family's shift there
    most often — the stable choice a GM makes (LOGIC-17)."""
    out = {}
    marked = {}
    for r in history or []:
        name = (r.get("employee") or "").strip()
        if not name or _family_of(c, r.get("role")) != family:
            continue
        part = _part_of(r)
        d = str(r.get("date") or "")[:10]
        if not part or not d:
            continue
        day = _stime.weekday_of(d)
        key = c.key(name) if c is not None and hasattr(c, "key") else name.lower()
        slot = out.setdefault((day, part), (None, {}))
        slot[1][key] = slot[1].get(key, 0) + 1
        notes = str(r.get("notes") or "")
        if NOTE_MARK in notes or str(rule.get("label") or "\0") in notes:
            if d > marked.get((day, part), ("", None))[0]:
                marked[(day, part)] = (d, key)
    for k, (_d, key) in marked.items():
        out[k] = (key, out.get(k, (None, {}))[1])
    return out


def requirement_adjustments(dplan) -> list:
    """[{date, daypart, role, delta, at_least, reason, firm: True}] — each
    placed row on its shift's requirement (schedule_requirements
    `adjustments`). A position the restaurant has been running is one of
    its role's usual crew (`_in_usual`): the requirement is the usual crew,
    at least this one (delta 0) — never a +1 on top of a crew that already
    counts it (LOGIC-16). A position newer than the history is one more."""
    out = []
    for r in (dplan or {}).get("rows") or []:
        part = _part_of(r)
        if not part:
            continue
        if r.get("_in_usual", True):
            out.append({"date": r["date"], "daypart": part, "role": r.get("role"), "delta": 0, "at_least": 1,
                        "reason": f"{r.get('_pin_reason') or 'standing position'} (one of the usual crew)",
                        "firm": True})
        else:
            out.append({"date": r["date"], "daypart": part, "role": r.get("role"), "delta": 1,
                        "reason": f"{r.get('_pin_reason') or 'standing position'} (one more, every such shift)",
                        "firm": True})
    return out


def prompt_block(dplan, dates=None) -> str:
    """STANDING POSITIONS — ALREADY SCHEDULED: what code adds, so the model
    neither writes it nor writes another shift for that person that day."""
    dplan = dplan or {}
    want = set(dates) if dates else None
    rows = [r for r in dplan.get("rows") or [] if want is None or r["date"] in want]
    unf = [u for u in dplan.get("unfilled") or [] if want is None or u["date"] in want]
    if not rows and not unf:
        return ""
    lines = [f"  {_stime.weekday_of(r['date'])[:3]} {r['date']}: {r['employee']} {r['shift_start']}–{r['shift_end']} "
             f"({r['role']}) — {r['_pin_reason']}" for r in sorted(rows, key=lambda r: (r["date"], r["shift_start"]))]
    lines += [f"  {_stime.weekday_of(u['date'])[:3]} {u['date']} {PART_WORDS[u['part']]}: {u['label']} could not be placed "
              f"— staff the role as usual; the owner is told" for u in unf]
    extra = any(r.get("_in_usual") is False for r in rows)
    count = ("Each is one MORE of its role on top of the usual crew, and SHIFT REQUIREMENTS already count it "
             "(the +1 marked with its name): write the rest of the role's number as usual." if extra else
             "Each is one of its role's usual crew on that shift, and SHIFT REQUIREMENTS already count it: write "
             "the rest of the role's number less this one.")
    return ("\n\nSTANDING POSITIONS — ALREADY SCHEDULED. The restaurant staffs these on every listed shift "
            "whatever the forecast says (" + "; ".join(dplan.get("rules") or []) + "). Code adds these rows to your "
            "answer exactly as listed: do NOT write them, and never write another shift for these people on these "
            "dates. " + count + "\n" + "\n".join(lines))


def review_lines(dplan) -> list:
    """What the owner reads about the positions this week: a slot nobody
    could take (with why) and a slot a note waived (with the note)."""
    from time_utils import mdy
    out = []
    for u in (dplan or {}).get("unfilled") or []:
        out.append(f"{u['label']} — {_stime.weekday_of(u['date'])} {mdy(u['date'])} {PART_WORDS[u['part']]} has nobody: "
                   f"{u['why']}.")
    for w in (dplan or {}).get("waived") or []:
        out.append(f"{w['label']} — not staffed {_stime.weekday_of(w['date'])} {mdy(w['date'])} {PART_WORDS[w['part']]}, "
                   f"as your note says: “{w['note']}”.")
    for u in (dplan or {}).get("unconfirmed_waivers") or []:
        out.append(f"{u['label']} — your note “{u['note']}” reads as letting it off, but you haven't confirmed that, "
                   f"so the position is staffed. Confirm it in the Schedule Studio's AI tab to let it off.")
    return out


def is_dedicated_note(notes) -> bool:
    return NOTE_MARK in str(notes or "")
