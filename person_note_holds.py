"""person_note_holds — a scheduling note about one person, held by code the
way availability is (owner, 10/2/26: "is employee availability the highest,
non-negotiable priority?" — it is; a note was only ever the model's to obey).

A note ("no Tuesdays until 10/31 — class", "out 12/20–12/28", "no nights",
"weekends only") is free text the draft is told to follow and nothing
checked. Here each of a person's notes is read (read_part) by an ALLOWLIST,
like schedule_note_rules: it is a HOLD only when every word is accounted for
— days, dates, lunch or dinner, and words that say "not then" or "only" — and
a reason after a spaced dash or in brackets is kept as the reason. The owner
confirms it (Labor → Scheduling notes), and from then on, inside its dates,
the person cannot be scheduled then: schedule_rules.Constraints.blocked_dates
(a whole day) or daypart_avail (a lunch or a dinner) — the same hard check
availability and approved time off use, in the draft, the solver, every fill
pass and the publish gate. Anything else ("max 25 hours", "not with Mike")
says it is not checked, and why.

Deterministic; no model reads the note. Never raises into a generation.
"""
import json
import re
from datetime import date, timedelta

from models import DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAYPARTS = ("morning", "night")
SOURCE_MAX = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_note_holds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    employee_key    TEXT    NOT NULL,
    employee_name   TEXT    NOT NULL,
    part_text       TEXT    NOT NULL,
    days            TEXT,
    dayparts        TEXT,
    start_date      TEXT,
    end_date        TEXT,
    created_by      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    removed_at      TEXT,
    removed_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_note_holds_rid ON staff_note_holds(restaurant_id, removed_at);
"""


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path and db_path != DB_PATH else models.get_conn()


def init_holds(db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _key(name):
    return " ".join(str(name or "").lower().split())


# ── reading one note ───────────────────────────────────────────────────────

_DAY_ABBR = {"mon": "Monday", "monday": "Monday", "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
             "wed": "Wednesday", "weds": "Wednesday", "wednesday": "Wednesday", "thu": "Thursday",
             "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday", "fri": "Friday", "friday": "Friday",
             "sat": "Saturday", "saturday": "Saturday", "sun": "Sunday", "sunday": "Sunday"}
_GROUPS = {"weekend": ("Saturday", "Sunday"), "weekends": ("Saturday", "Sunday"),
           "weekday": DAYS[:5], "weekdays": DAYS[:5], "weeknight": DAYS[:4], "weeknights": DAYS[:4]}
_PARTS = {"lunch": "morning", "lunches": "morning", "brunch": "morning", "breakfast": "morning", "morning": "morning",
          "mornings": "morning", "am": "morning", "days": "morning", "daytime": "morning", "opening": "morning",
          "opens": "morning", "dinner": "night", "dinners": "night", "night": "night", "nights": "night",
          "nite": "night", "nites": "night", "evening": "night", "evenings": "night", "pm": "night",
          "close": "night", "closes": "night", "closing": "night", "closings": "night"}
_NOT = {"no", "not", "off", "out", "cant", "cannot", "unavailable", "never", "wont", "doesnt", "isnt", "away",
        "vacation", "pto", "leave", "gone", "unable", "busy"}
_ONLY = {"only", "just"}
_FILLER = {"on", "the", "a", "an", "any", "and", "or", "of", "for", "in", "at", "is", "are", "be", "work", "works",
           "working", "shifts", "shift", "available", "can", "he", "she", "they", "them", "her", "his", "their",
           "until", "till", "thru", "through", "to", "from", "starting", "after", "before", "will", "be", "back",
           "every", "each", "all", "week", "weeks", "s", "t", "d", "ll", "please", "schedule", "scheduled", "do",
           "does", "need", "needs", "with", "this", "next", "but"}


def _norm(text):
    low = " ".join(str(text or "").lower().replace("’", "'").replace("‘", "'").split())
    low = low.replace("a.m.", " am ").replace("p.m.", " pm ").replace("&", " and ")
    low = re.sub(r"\b(can|won|doesn|isn|don)'?t\b", lambda m: m.group(1) + "t", low)
    low = re.sub(r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)\.", r"\1", low)
    # "Fri-Sun", "M-F", "Su-Th": a run of days, never two days.
    for rx, rep in ((r"\bm\s*-\s*f\b", "monday to friday"), (r"\bm\s*-\s*th\b", "monday to thursday"),
                    (r"\bsu\s*-\s*th\b", "sunday to thursday")):
        low = re.sub(rx, rep, low)
    _d = r"(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)[a-z]*"
    low = re.sub(r"\b" + _d + r"\s*[-–]\s*" + _d + r"\b", r"\1 to \2", low)
    return low


def _split_reason(text):
    """("no tuesdays", "class") from "no Tuesdays — class" or "(class)"."""
    t = " ".join(str(text or "").split())
    m = re.search(r"\s[—–-]\s|\(|\bbecause\b|\bfor (?:school|class|work)\b", t)
    if not m:
        return t, None
    head, reason = t[:m.start()].strip(), t[m.start():].strip(" —–-():").strip(" )")
    return head, reason or None


def _date_of(m, d, y, ref):
    """M/D[/Y] → a date on or after ref - 30 days (a note written in
    December about 1/5 means next January)."""
    mo, dy = int(m), int(d)
    if y:
        yr = int(y)
        yr = yr + 2000 if yr < 100 else yr
        return date(yr, mo, dy)
    cand = date(ref.year, mo, dy)
    if cand < ref - timedelta(days=30):
        cand = date(ref.year + 1, mo, dy)
    return cand


_DATE_RX = r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?"


def read_part(text, noted=None, expires=None, today=None) -> dict:
    """{"text", "kind": "hold" | "unchecked", "why", "reason"?, "hold"?:
    {"days", "dayparts", "start", "end", "words"}} for one note."""
    today = today or date.today()
    ref = date.fromisoformat(str(noted)[:10]) if noted else today
    head, reason = _split_reason(text)
    out = {"text": " ".join(str(text or "").split()), "reason": reason}
    low = _norm(head)
    if not low:
        return dict(out, kind="unchecked", why="Nothing to read.")
    start = end = None
    # dates: a range, "until D", a single date
    rng = re.search(_DATE_RX + r"\s*(?:-|–|to|thru|through|till|until)\s*" + _DATE_RX, low)
    if rng:
        try:
            start = _date_of(rng.group(1), rng.group(2), rng.group(3), ref)
            end = _date_of(rng.group(4), rng.group(5), rng.group(6), start)
        except ValueError:
            return dict(out, kind="unchecked", why="A date in it isn't a real date.")
        low = low[:rng.start()] + " " + low[rng.end():]
    m = re.search(r"\b(?:until|till|thru|through|before)\s+" + _DATE_RX, low)
    if m and end is None:
        try:
            end = _date_of(m.group(1), m.group(2), m.group(3), ref)
        except ValueError:
            return dict(out, kind="unchecked", why="A date in it isn't a real date.")
        low = low[:m.start()] + " " + low[m.end():]
    m = re.search(r"\b(?:starting|from|after)\s+" + _DATE_RX, low)
    if m and start is None:
        try:
            start = _date_of(m.group(1), m.group(2), m.group(3), ref)
        except ValueError:
            return dict(out, kind="unchecked", why="A date in it isn't a real date.")
        low = low[:m.start()] + " " + low[m.end():]
    singles = list(re.finditer(_DATE_RX, low))
    if len(singles) == 1 and start is None and end is None:
        try:
            start = end = _date_of(singles[0].group(1), singles[0].group(2), singles[0].group(3), ref)
        except ValueError:
            return dict(out, kind="unchecked", why="A date in it isn't a real date.")
        low = low[:singles[0].start()] + " " + low[singles[0].end():]
    elif singles:
        return dict(out, kind="unchecked", why="It names more than one date — write a range, like “out 12/20–12/28”.")
    if re.search(r"\d", low):
        return dict(out, kind="unchecked", why="It has a number Cavnar AI can't hold (hours, a time, a count) — "
                                               "a hold is days, dates, lunch or dinner.")
    tokens = re.findall(r"[a-z]+", low)
    days, parts, leftover = [], [], []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        d = _DAY_ABBR.get(t) or _DAY_ABBR.get(t.rstrip("s"))
        if d and i + 2 < len(tokens) and tokens[i + 1] in ("to", "thru", "through", "till") and \
                (_DAY_ABBR.get(tokens[i + 2]) or _DAY_ABBR.get(tokens[i + 2].rstrip("s"))):
            b = _DAY_ABBR.get(tokens[i + 2]) or _DAY_ABBR.get(tokens[i + 2].rstrip("s"))
            k = DAYS.index(d)
            while True:
                days.append(DAYS[k])
                if DAYS[k] == b:
                    break
                k = (k + 1) % 7
            i += 3
            continue
        if d:
            days.append(d)
        elif t in _GROUPS:
            days.extend(_GROUPS[t])
            if t.startswith("weeknight"):
                parts.append("night")
        elif t in _PARTS:
            parts.append(_PARTS[t])
        elif t in _NOT or t in _ONLY or t in _FILLER:
            pass
        else:
            leftover.append(t)
        i += 1
    if leftover:
        said = ", ".join(f"“{w}”" for w in dict.fromkeys(leftover))
        return dict(out, kind="unchecked",
                    why=f"Cavnar AI can't hold the part about {said} — a hold is days, dates, lunch or dinner off. "
                        "The draft still reads the note.")
    neg = any(t in _NOT for t in tokens)
    only = any(t in _ONLY for t in tokens)
    days = [d for d in DAYS if d in set(days)]
    parts = [p for p in DAYPARTS if p in set(parts)]
    if not (days or parts or start or end):
        return dict(out, kind="unchecked", why="It names no day, date, lunch or dinner to keep them off.")
    if only and neg:
        return dict(out, kind="unchecked", why="It says both “only” and “not” — write one.")
    if only:
        # "weekends only", "nights only": off everything else.
        days = [d for d in DAYS if d not in days] if days else None
        parts = [p for p in DAYPARTS if p not in parts] if parts else None
        if not days and not parts:
            return dict(out, kind="unchecked", why="Nothing would be left to keep them off.")
    elif not neg and not (start and not days and not parts):
        return dict(out, kind="unchecked", why="It doesn't say they can't work — say “no …”, “off …” "
                                               "or “only …”.")
    days = days or None
    parts = parts or None
    if parts and len(parts) == 2:
        parts = None
    if expires and (end is None or str(expires)[:10] < end.isoformat()):
        end = date.fromisoformat(str(expires)[:10])
    if start is None:
        start = ref                      # from the day the note was written
    hold = {"days": days, "dayparts": parts, "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None}
    hold["words"] = words_for(hold)
    return dict(out, kind="hold", hold=hold)


def mentioned(text, noted=None, today=None) -> dict:
    """{"days", "start", "end", "neg", "only"} — the weekdays and dates a note
    names, and whether it says "not" or "only", however much else it says.
    read_part holds a note only when every word is accounted for; this is
    for the rest ("not with Mike on Fridays", "out 12/20-12/28 probably"),
    which nobody confirmed and the fill passes still keep clear of."""
    today = today or date.today()
    try:
        ref = date.fromisoformat(str(noted)[:10]) if noted else today
    except ValueError:
        ref = today
    head, _reason = _split_reason(text)
    low = _norm(head)
    start = end = None
    try:
        rng = re.search(_DATE_RX + r"\s*(?:-|–|to|thru|through|till|until)\s*" + _DATE_RX, low)
        if rng:
            start = _date_of(rng.group(1), rng.group(2), rng.group(3), ref)
            end = _date_of(rng.group(4), rng.group(5), rng.group(6), start)
            low = low[:rng.start()] + " " + low[rng.end():]
        else:
            m = re.search(r"\b(?:until|till|thru|through|before)\s+" + _DATE_RX, low)
            if m:
                end = _date_of(m.group(1), m.group(2), m.group(3), ref)
                low = low[:m.start()] + " " + low[m.end():]
            singles = list(re.finditer(_DATE_RX, low))
            if len(singles) == 1 and end is None:
                start = end = _date_of(singles[0].group(1), singles[0].group(2), singles[0].group(3), ref)
    except ValueError:
        start = end = None
    tokens = re.findall(r"[a-z]+", low)
    days = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        d = _DAY_ABBR.get(t) or _DAY_ABBR.get(t.rstrip("s"))
        b = (_DAY_ABBR.get(tokens[i + 2]) or _DAY_ABBR.get(tokens[i + 2].rstrip("s"))) if (
            d and i + 2 < len(tokens) and tokens[i + 1] in ("to", "thru", "through", "till")) else None
        if b:
            k = DAYS.index(d)
            while True:
                days.append(DAYS[k])
                if DAYS[k] == b:
                    break
                k = (k + 1) % 7
            i += 3
            continue
        if d:
            days.append(d)
        elif t in _GROUPS:
            days.extend(_GROUPS[t])
        i += 1
    return {"days": [d for d in DAYS if d in set(days)], "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None, "neg": any(t in _NOT for t in tokens),
            "only": any(t in _ONLY for t in tokens)}


def caution(text, noted=None, expires=None, week_dates=(), today=None):
    """{"days": set, "dates": set, "parts": set} a note nobody has confirmed keeps the
    fill passes off (schedule audit 10/3/26 D-34), or None. A note the
    reader can hold ("can't close Fridays") cautions the days it names;
    one it can't ("not with Mike on Fridays") cautions the days or dates it
    names when it says "not" (or, with "only", the other days). A note
    naming no day or date ("max 25 hours", "prefers bar") cautions none.
    `dates` are the dates of `week_dates` it covers; `days` its weekdays
    when it has no end — for a reader with no week. The whole day, never a
    part: the note is unconfirmed, so the fills keep well clear of it."""
    r = read_part(text, noted=noted, expires=expires, today=today)
    parts = set()
    if r.get("kind") == "hold":
        h = r["hold"]
        days, start, end = list(h.get("days") or []), h.get("start"), h.get("end")
        parts = set(h.get("dayparts") or [])
    else:
        m = mentioned(text, noted=noted, today=today)
        if not (m["neg"] or m["only"]) or not (m["days"] or m["start"] or m["end"]):
            return None
        if m["neg"] and m["only"]:
            return None
        days, start, end = list(m["days"]), m["start"], m["end"]
        if m["only"]:
            if not days:
                return None
            days = [d for d in DAYS if d not in days]
        if expires and (end is None or str(expires)[:10] < end):
            end = str(expires)[:10]
    dates = set()
    for d in week_dates or ():
        d = str(d)[:10]
        if (start and d < start) or (end and d > end):
            continue
        try:
            wd = date.fromisoformat(d).strftime("%A")
        except ValueError:
            continue
        if days and wd not in days:
            continue
        dates.add(d)
    week_days = set(days) if (days and not end) else (set(DAYS) if (not days and not end and not start) else set())
    if not dates and not week_days:
        return None
    # `parts`: the lunch or dinner a held reading names ("no nights") — a
    # fill that says which daypart it is filling may use the other one.
    return {"days": week_days, "dates": dates, "parts": parts}


def words_for(h) -> str:
    """"off Tuesdays at dinner, 10/2/26–10/31/26"."""
    from time_utils import mdy
    days = h.get("days")
    if not days:
        on = "every day"
    elif list(days) == ["Saturday", "Sunday"]:
        on = "weekends"
    elif list(days) == list(DAYS[:5]):
        on = "weekdays"
    else:
        on = ", ".join(d[:3] for d in days)
    when = {"morning": " at lunch", "night": " at dinner"}.get((h.get("dayparts") or [None])[0], "") \
        if h.get("dayparts") else ""
    if h.get("start") and h.get("end"):
        span = mdy(h["start"]) if h["start"] == h["end"] else f"{mdy(h['start'])}–{mdy(h['end'])}"
    elif h.get("end"):
        span = f"until {mdy(h['end'])}"
    elif h.get("start"):
        span = f"from {mdy(h['start'])} on"
    else:
        span = "from now on"
    return f"off {on}{when}, {span}"


# ── the holds ──────────────────────────────────────────────────────────────

def _row(r):
    d = dict(r)
    d["days"] = json.loads(d["days"]) if d.get("days") else None
    d["dayparts"] = json.loads(d["dayparts"]) if d.get("dayparts") else None
    d["start"], d["end"] = d.get("start_date"), d.get("end_date")
    d["words"] = words_for(d)
    return d


def holds(restaurant_id, db_path=DB_PATH, include_ended=False, today=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM staff_note_holds WHERE restaurant_id=? AND removed_at IS NULL ORDER BY id",
            (restaurant_id,)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
    if include_ended:
        return rows
    t = (today or date.today()).isoformat()
    return [h for h in rows if not h.get("end") or h["end"] >= t]


def add_hold(restaurant_id, employee_name, part_text, days=None, dayparts=None, start=None, end=None,
             user=None, db_path=DB_PATH) -> dict:
    """Store one confirmed hold. Raises ValueError with the owner's words."""
    name = " ".join(str(employee_name or "").split())[:80]
    if not name:
        raise ValueError("Whose note is this?")
    bad = [d for d in (days or []) if d not in DAYS]
    if bad:
        raise ValueError(f"{bad[0]} isn't a day — use Monday to Sunday.")
    if any(p not in DAYPARTS for p in (dayparts or [])):
        raise ValueError("Lunch, dinner or the whole day.")
    for v in (start, end):
        if v:
            try:
                date.fromisoformat(str(v)[:10])
            except ValueError:
                raise ValueError("That isn't a date.")
    if start and end and str(end)[:10] < str(start)[:10]:
        raise ValueError("The end is before the start.")
    if not (days or dayparts or start or end):
        raise ValueError("A hold needs a day, a date, lunch or dinner.")
    text = " ".join(str(part_text or "").split())[:SOURCE_MAX]
    for h in holds(restaurant_id, db_path=db_path, include_ended=True):
        if h["employee_key"] == _key(name) and _key(h["part_text"]) == _key(text):
            return dict(h, existing=True)
    who = ((user or {}).get("email") or (user or {}).get("username")) if user else None
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO staff_note_holds (restaurant_id, employee_key, employee_name, part_text, days, dayparts, "
            "start_date, end_date, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, _key(name), name, text, json.dumps(list(days)) if days else None,
             json.dumps(list(dayparts)) if dayparts else None, str(start)[:10] if start else None,
             str(end)[:10] if end else None, who))
        conn.commit()
        row = conn.execute("SELECT * FROM staff_note_holds WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return _row(row)


def remove_hold(restaurant_id, hold_id, user=None, db_path=DB_PATH):
    who = ((user or {}).get("email") or (user or {}).get("username")) if user else None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM staff_note_holds WHERE id=? AND restaurant_id=? AND removed_at IS NULL",
                           (int(hold_id), restaurant_id)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE staff_note_holds SET removed_at=datetime('now'), removed_by=? WHERE id=?",
                     (who, int(hold_id)))
        conn.commit()
        return _row(row)
    finally:
        conn.close()


def readings(restaurant_id, notes, today=None, db_path=DB_PATH) -> list:
    """Each note part of get_staff_notes rows, with its reading and the hold
    made from it (`held`) — what Labor → Scheduling notes draws."""
    have = {(h["employee_key"], _key(h["part_text"])): h for h in holds(restaurant_id, db_path=db_path,
                                                                         include_ended=True)}
    for n in notes or []:
        for p in n.get("parts") or []:
            r = read_part(p.get("text"), noted=p.get("noted_on") or p.get("noted"), expires=p.get("expires_on")
                          or p.get("expires"), today=today)
            p["reading"] = {k: v for k, v in r.items() if k != "text"}
            h = have.get((_key(n.get("employee_name")), _key(p.get("text"))))
            if h:
                p["held"] = {"id": h["id"], "words": h["words"]}
    return notes


def apply_holds(c, restaurant_id, db_path=DB_PATH):
    """Make every hold in force for the dates being built (c.week_dates) a
    hard unavailability: a whole day into c.blocked_dates (the reason names
    the note), a lunch or a dinner into c.daypart_avail for that weekday —
    only when every date being built is inside the hold, since daypart
    availability is by weekday. Never raises."""
    try:
        if not c.week_dates:
            return
        first, last = min(c.week_dates), max(c.week_dates)
        for h in holds(restaurant_id, db_path=db_path, include_ended=True):
            if (h.get("end") and h["end"] < first) or (h.get("start") and h["start"] > last):
                continue
            # Filed under the person's one key (Constraints.key — people's
            # identity): a hold kept under "Mike" holds for the roster's
            # "Michael" (schedule audit 10/3/26 D-8).
            key = c.key(h["employee_name"]) if hasattr(c, "key") else h["employee_key"]
            reason = "your note: " + h["part_text"][:120]
            inside = all((not h.get("start") or d >= h["start"]) and (not h.get("end") or d <= h["end"])
                         for d in c.week_dates)
            for d in c.week_dates:
                if (h.get("start") and d < h["start"]) or (h.get("end") and d > h["end"]):
                    continue
                wd = date.fromisoformat(d).strftime("%A")
                if h.get("days") and wd not in h["days"]:
                    continue
                if not h.get("dayparts"):
                    c.blocked_dates.setdefault(key, {})[d] = reason
                elif inside:
                    off = h["dayparts"][0]
                    keep = "night" if off == "morning" else "morning"
                    cur = (c.daypart_avail.get(key) or {}).get(wd)
                    c.daypart_avail.setdefault(key, {})[wd] = "off" if cur in (off, "off") else keep
                elif hasattr(c, "blocked_parts"):
                    # Only part of the week is inside the hold: its lunch or
                    # dinner is off on those dates alone (Constraints.
                    # blocked_parts, schedule audit 10/3/26 D-39) — it was
                    # not applied at all, the weekday map being by weekday.
                    c.blocked_parts.setdefault(key, {}).setdefault(d, []).append(
                        {"from": None, "until": None, "daypart": h["dayparts"][0], "reason": reason})
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="person_note_holds", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
