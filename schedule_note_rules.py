"""schedule_note_rules — the Studio's AI notes, turned into rules code enforces
(owner, 10/1/26: "I don't want them to say 'I typed something here and it
didn't do what I said'").

The notes ("how you like the week built", restaurants.sched_notes) reach the
draft as text, and nothing checked the draft against them. Here each
sentence is read (read_notes):

  rule        a minimum the code can hold: "keep two cooks on Friday lunch",
              "at least 2 bartenders Saturday night", "never cut the host".
              The owner confirms it — every week, or the one week being
              drafted — and from then on it is a role floor
              (apply_note_rules → Constraints.role_floors): the draft is built
              to it (schedule_engine._ensure_role_floors), the trim never
              goes under it, and a draft short of it is a coverage_floor
              breach that names the note.
  unchecked   about staffing, in a shape no code can hold yet (a maximum,
              "one more bartender", "game days"): said plainly, so the owner
              checks the draft or turns it into a minimum by hand.
  person      about one person by name: pointed to their dated scheduling
              notes (a hard constraint), not made a floor.
  guidance    everything else: the draft aims for it; nothing checks it.

Deterministic — no model reads the notes here, so what the owner confirms is
exactly what is enforced. Never raises into a generation.
"""
import json
import re
from datetime import date, timedelta

from models import DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAYPARTS = ("morning", "night")
DAYPART_WORDS = {"morning": "lunch", "night": "dinner"}
MIN_MAX = 10
SOURCE_MAX = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule_note_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    scope           TEXT    NOT NULL CHECK(scope IN ('every','week')),
    week_start      TEXT,
    role            TEXT    NOT NULL,
    min_people      INTEGER NOT NULL,
    dayparts        TEXT    NOT NULL,
    days            TEXT,
    source_text     TEXT,
    created_by      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    removed_at      TEXT,
    removed_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_note_rules_rid ON schedule_note_rules(restaurant_id, removed_at);
"""


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path and db_path != DB_PATH else models.get_conn()


def init_note_rules(db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ── reading the notes ───────────────────────────────────────────────────────

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "both": 2, "a couple": 2, "couple": 2}
_NUM_RX = r"(\d+|a couple(?: of)?|couple(?: of)?|an|a|one|two|three|four|five|six|seven|eight|nine|ten)"
_STAFF_WORDS = re.compile(r"\b(staff\w*|shifts?|cover(?:s|age)?|clos(?:e|es|er|ers|ing)|open(?:er|ers|ing)?|"
                          r"cut|send home|on the floor|people|person|crew|team|schedul\w*|hours)\b")
_MAX_RX = re.compile(r"\b(no more than|at most|maximum|max|cap|capped|never more than|only|up to|limit)\b")
_RELATIVE_RX = re.compile(r"\b(more|extra|another|additional|fewer|less|double)\b")
_EVENT_DAYS_RX = re.compile(r"\b(game ?days?|game ?nights?|event ?days?|event ?nights?|holidays?|busy (?:days|nights))\b")
_WEEK_RX = re.compile(r"\b(this week|next week|this coming week|the week of|for the week)\b")
# First names that are also everyday words, never read as a name opening a
# sentence ("Will need two cooks").
_COMMON_WORDS = {"will", "may", "june", "april", "august", "rose", "mark", "bill", "pat", "sue", "art", "max",
                 "grant", "chase", "hope", "joy", "summer", "dawn", "faith", "frank", "jack", "don", "ray"}
_ALL_DAY_RX = re.compile(r"\b(at all times|all day|every shift|both shifts|lunch and dinner|day and night|all shifts)\b")


def _sentences(text):
    out = []
    for line in str(text or "").replace("\r", "\n").split("\n"):
        line = line.strip(" -•*\t")
        for part in re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9\"'“])|;\s*", line):
            part = part.strip()
            if len(part.strip(" .!?;")) >= 3:
                out.append(part)
    return out


def _singular(w):
    w = w.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("es") and w[:-2].endswith(("sh", "ch", "ss", "x")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _match_roles(low, roles):
    """(role, choices, the words that named it): an exact role name first
    (longest), else the roles whose last word the sentence names ("cooks" →
    Line Cook and Prep Cook: two choices, the owner picks)."""
    clean = sorted({str(r).strip() for r in roles or () if str(r or "").strip()}, key=len, reverse=True)
    for r in clean:
        if re.search(r"\b" + re.escape(r.lower()) + r"(?:s|es)?\b", low):
            return r, [r], r.lower()
    words = {_singular(w) for w in re.findall(r"[a-z]+", low)}
    hits = [r for r in clean if _singular(r.lower().split()[-1]) in words]
    if not hits:
        return None, [], None
    word = _singular(hits[0].lower().split()[-1])
    return (hits[0] if len(hits) == 1 else None), hits, word


_DAY_ABBR = {"mon": "Monday", "monday": "Monday", "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
             "wed": "Wednesday", "weds": "Wednesday", "wednesday": "Wednesday", "thu": "Thursday",
             "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday", "fri": "Friday", "friday": "Friday",
             "sat": "Saturday", "saturday": "Saturday", "sun": "Sunday", "sunday": "Sunday"}
_DAY_WORD = r"(mon(?:day)?|tues?(?:day)?|weds?(?:nesday)?|thu(?:rs?)?(?:day)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?"
_DATE_RX = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")


def _day_of(word):
    w = word.lower().rstrip("s") if word.lower() not in ("tues", "thurs", "weds") else word.lower()
    return _DAY_ABBR.get(w) or _DAY_ABBR.get(word.lower())


def _days(low):
    """The weekdays a sentence names: single days and abbreviations (Tues,
    Thurs), ranges ("Fri-Sun", "Thursday through Saturday", wrapping
    "Sun to Thu"), weekends / weekdays / weeknights, and "every day except
    Monday". None for every day."""
    days = set()
    for m in re.finditer(r"\b" + _DAY_WORD + r"\s*(?:-|–|to|through|thru|till|until)\s*" + _DAY_WORD + r"\b", low):
        a, b = _day_of(m.group(1)), _day_of(m.group(2))
        if a and b:
            i = DAYS.index(a)
            while True:
                days.add(DAYS[i])
                if DAYS[i] == b:
                    break
                i = (i + 1) % 7
        low = low.replace(m.group(0), " ")
    for m in re.finditer(r"\b" + _DAY_WORD + r"\b", low):
        d = _day_of(m.group(1))
        if d:
            days.add(d)
    if re.search(r"\bweekends?\b", low):
        days |= {"Saturday", "Sunday"}
    if re.search(r"\bweekdays?\b", low):
        days |= {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
    if re.search(r"\bweeknights?\b", low):
        days |= {"Monday", "Tuesday", "Wednesday", "Thursday"}
    m = re.search(r"\b(?:except|but|other than|besides)\s+(?:on\s+)?(.+)$", low)
    if m:
        out_days = set()
        tail = m.group(1)
        for mm in re.finditer(r"\b" + _DAY_WORD + r"\b", tail):
            d = _day_of(mm.group(1))
            if d:
                out_days.add(d)
        if re.search(r"\bweekends?\b", tail):
            out_days |= {"Saturday", "Sunday"}
        if out_days:
            return [d for d in DAYS if d not in out_days]
    return [d for d in DAYS if d in days] or None


def _dayparts(low):
    import schedule_rules
    has_m = any(re.search(r"\b" + w + r"\b", low) for w, p in schedule_rules._RULE_DAYPARTS.items() if p == "morning")
    has_n = any(re.search(r"\b" + w + r"\b", low) for w, p in schedule_rules._RULE_DAYPARTS.items() if p == "night") \
        or bool(re.search(r"\bweeknights?\b", low))
    # "on lunch at all times" is lunch: a named daypart wins over "all times".
    if has_m and has_n:
        return ["morning", "night"]
    if has_m:
        return ["morning"]
    if has_n:
        return ["night"]
    if _ALL_DAY_RX.search(low):
        return ["morning", "night"]
    return None


# What makes a sentence NOT a plain minimum (blind audit, 10/1/26): each is
# said as "not checked", with why, never offered as a rule that would mean
# something else.
_NEG_RX = re.compile(r"(n't\b|\b(?:dont|cant|cannot|wont|shouldnt|isnt|arent|too many|too much|overkill|"
                     r"is enough|are enough|plenty)\b)")
_NO_RX = re.compile(r"\b(no|not|without|zero|none)\b")
_MIN_NEG_OK = re.compile(r"\b(no fewer than|not fewer than|no less than|not less than|never (?:go |drop |run )?"
                         r"(?:below|under|fewer than|less than)|never (?:cut|send home|drop|remove|skip|schedule "
                         r"without|go without|run without))\b")
_COND_RX = re.compile(r"\b(if|when|whenever|unless|in case|depending|depends|game|games|playoffs?|holidays?|"
                      r"rains?|rainy|snow|busy|slow|events?|concerts?|parties|party|catering|"
                      r"mother's day|father's day|valentine's|new year's|christmas|thanksgiving|easter)\b")
_HEDGE_RX = re.compile(r"\b(ideally|if possible|try to|trying to|would like|we'd like|prefer|preferably|maybe|"
                       r"might|perhaps|used to|usually|sometimes|normally|typically|hopefully)\b")
_CUT_RX = re.compile(r"\b(send\b.*\bhome|cut (?:to|a|an|one|down|back|\d)|let\b.*\bgo\b|go home|leave early|"
                     r"cut\b.*\bat\b)\b")
_TIME_RX = re.compile(r"(\b(?:after|before|until|till|by|from|at|around|starting|start at)\s+\d|\d\s*(?:am|pm)\b|"
                      r"\d:\d\d|\bhappy hour\b|\buntil close\b|\bat close\b|\blate night\b|\bopening\b|"
                      r"\bclosing time\b)")
_PART_OF_RX = re.compile(r"\b\d+\s+of\s+(?:our|the|my|them|those|these)\b|\bof (?:our|the) \d+\b")
_MIN_VERB_RX = re.compile(r"\b(keep|keeps|need|needs|needed|at least|always|never|minimum|min|must|schedule|"
                          r"staff|put|want|open with|close with|run with|cover|have at least|no fewer|not fewer|"
                          r"no less|not less|require|requires|required|make sure)\b")


def _number_before(low, word):
    """The count said right before the role word ("two cooks", "2 line
    cooks", "a bartender"), allowing one word between ("two good cooks")."""
    if not word:
        return None
    w = re.escape(word)
    m = re.search(r"\b" + _NUM_RX + r"\s+(?:[a-z]+\s+)?" + w + r"(?:s|es)?\b", low)
    if not m:
        return None
    raw = m.group(1).replace(" of", "").strip()
    return int(raw) if raw.isdigit() else _NUM.get(raw)


def _role_mentions(low, roles):
    """Every distinct role the sentence names (full names first, then last
    words), for spotting a sentence that names more than one."""
    found, rest = [], low
    for r in sorted({str(x).strip() for x in roles or () if str(x or "").strip()}, key=len, reverse=True):
        if re.search(r"\b" + re.escape(r.lower()) + r"(?:s|es)?\b", rest):
            found.append(r.lower())
            rest = re.sub(r"\b" + re.escape(r.lower()) + r"(?:s|es)?\b", " ", rest)
    words = {_singular(w) for w in re.findall(r"[a-z]+", rest)}
    for r in roles or ():
        last = _singular(str(r).lower().split()[-1])
        if last in words and last not in found:
            found.append(last)
    return found


def _person(text, low, names):
    """A roster name the sentence is about: a multi-word name in full, or a
    first name capitalised — never an everyday word (Max, May, Will,
    Grant) unless it is the whole of a multi-word name."""
    raw = " ".join(str(text or "").split())
    for nm in sorted({str(x).strip() for x in names or () if str(x or "").strip()}, key=len, reverse=True):
        parts = nm.split()
        first = parts[0]
        if len(parts) > 1 and re.search(r"\b" + re.escape(nm.lower()) + r"\b", low):
            return nm
        if first.lower() in _COMMON_WORDS or len(first) < 3:
            continue
        if re.search(r"(?<![A-Za-z])" + re.escape(first.capitalize()) + r"(?:'s)?\b", raw):
            return nm
    return None


def _unchecked(out, why):
    return dict(out, kind="unchecked", why=why)


def read_sentence(text, roles, names=()) -> dict:
    """{"text", "kind": "rule" | "unchecked" | "person" | "guidance", "why",
    "rule"?} for one sentence of the notes. A rule carries {"role" (None
    when two roles fit), "role_choices", "min", "dayparts", "days", "scope"
    ("every" | "week")} — the owner's to confirm or change. Anything that
    could mean something other than "at least N of a role on these
    shifts" is unchecked, with why — never a rule that means something
    else (blind audit, 10/1/26)."""
    import schedule_rules
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    out = {"text": " ".join(str(text).split())}
    # About one person: their dated scheduling notes, a hard rule there.
    person = _person(text, low, names)
    if person:
        return dict(out, kind="person", person=person,
                    why=f"It's about {person} — put it in their scheduling notes, where it's a hard rule with a date.")
    role, choices, word = _match_roles(low, roles)
    staffing = bool(choices) or bool(_STAFF_WORDS.search(low))
    if not choices:
        if staffing:
            return _unchecked(out, "It doesn't name one of your roles, so no rule can be made from it.")
        return dict(out, kind="guidance", why="Not about who works when — the draft reads it; nothing checks it.")
    if _MAX_RX.search(low):
        return _unchecked(out, "A most-people limit — Cavnar AI can hold a minimum, not a maximum yet.")
    if _NEG_RX.search(low) or (_NO_RX.search(low) and not _MIN_NEG_OK.search(low)) or \
            (re.search(r"\bnever\b", low) and not _MIN_NEG_OK.search(low)):
        return _unchecked(out, "It says what not to do — a rule can only say how many must be on.")
    if _CUT_RX.search(low):
        return _unchecked(out, "It's about cutting or sending people home — a rule holds who must be on, not who goes.")
    if _TIME_RX.search(low):
        return _unchecked(out, "It names an hour of the day — a rule holds lunch or dinner, not a time.")
    if _DATE_RX.search(low):
        return _unchecked(out, "It names a date — make a rule for that week and pick its day.")
    if _COND_RX.search(low) or _EVENT_DAYS_RX.search(low):
        return _unchecked(out, "It depends on something (a game, the weather, a holiday) — make a rule for the "
                               "week it applies and pick the days.")
    if _HEDGE_RX.search(low):
        return _unchecked(out, "It's a preference, not a must — make it a rule if it must always hold.")
    if _PART_OF_RX.search(low):
        return _unchecked(out, "It's about part of a group, not how many are on.")
    counts = re.findall(r"\b" + _NUM_RX + r"\s+(?:[a-z]+\s+)?" + re.escape(word or "~") + r"(?:s|es)?\b", low)
    if len(_role_mentions(low, roles)) > 1 or len(counts) > 1:
        return _unchecked(out, "It names more than one role or count — make one rule for each.")
    n = None
    parsed = schedule_rules.parse_owner_rule(text, list(choices) + ([word] if word else []))
    if parsed:
        n = parsed["min"]
    if n is None:
        starts = re.match(r"^\s*" + _NUM_RX + r"\s+(?:[a-z]+\s+)?" + re.escape(word or "~") + r"(?:s|es)?\b", low)
        if _MIN_VERB_RX.search(low) or starts:
            n = _number_before(low, word)
        elif _number_before(low, word):
            return _unchecked(out, "It reads as a fact, not a must — say \u201ckeep at least …\u201d to make "
                                   "it a rule.")
    if not n and _RELATIVE_RX.search(low):
        return _unchecked(out, "It's relative to the usual (more, extra, fewer) — say the number you want on, "
                               "like \u201cat least 3\u201d.")
    if n is not None and _RELATIVE_RX.search(low) and not parsed:
        return _unchecked(out, "It's relative to the usual (more, extra, fewer) — say the number you want on, "
                               "like \u201cat least 3\u201d.")
    if not n:
        return _unchecked(out, "No number of people Cavnar AI can hold the draft to.")
    rule = {"role": role, "role_choices": choices, "min": min(int(n), MIN_MAX),
            "dayparts": _dayparts(low) or ["morning", "night"], "days": _days(low),
            "scope": "week" if _WEEK_RX.search(low) else "every"}
    why = None
    if role is None:
        why = f"More than one of your roles fits \u201c{word}\u201d — pick which."
    elif re.search(r"\bweeknights?\b", low):
        why = "\u201cWeeknights\u201d is read as Monday to Thursday — change the days if you mean otherwise."
    return dict(out, kind="rule", rule=rule, why=why)


def read_notes(restaurant_id, text, week_start=None, db_path=DB_PATH) -> list:
    """Every sentence of the notes, read (read_sentence), each with the
    active rule made from it (`rule_id`, `enforced` words) when there is one."""
    roles = restaurant_roles(restaurant_id, db_path=db_path)
    names = []
    try:
        import staff_settings
        names = [str(e.get("name") or "").strip() for e in staff_settings.roster(restaurant_id, db_path=db_path)
                 if e.get("active") is not False]
    except Exception:
        pass
    active = {}
    for r in note_rules(restaurant_id, week_start=week_start, db_path=db_path):
        active.setdefault(_key(r.get("source_text")), r)
    out = []
    for s in _sentences(text):
        item = read_sentence(s, roles, names)
        hit = active.get(_key(s))
        if hit:
            item["rule_id"], item["enforced"] = hit["id"], hit["words"]
        out.append(item)
    return out


def _key(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def restaurant_roles(restaurant_id, db_path=DB_PATH) -> list:
    """The restaurant's own role names: the roster's, the floors'. Never raises."""
    roles = set()
    try:
        import staff_settings
        roles |= {str(e.get("role") or "").strip() for e in staff_settings.roster(restaurant_id, db_path=db_path)
                  if e.get("role")}
    except Exception:
        pass
    try:
        import schedule_rules
        from models import get_restaurant
        roles |= {str(r).strip() for r in schedule_rules.role_floors(get_restaurant(restaurant_id, db_path))}
    except Exception:
        pass
    roles.discard("")
    return sorted(roles, key=str.lower)


# ── the rules ──────────────────────────────────────────────────────────────

def _monday(iso):
    d = date.fromisoformat(str(iso)[:10])
    return d - timedelta(days=d.weekday())


def words_for(rule) -> str:
    """"at least 2 Line Cook at lunch and dinner, Fridays · every week"."""
    from time_utils import mdy
    parts = rule.get("dayparts") or list(DAYPARTS)
    when = " and ".join(DAYPART_WORDS[p] for p in DAYPARTS if p in parts)
    days = rule.get("days")
    if not days:
        on = "every day"
    elif sorted(days) == sorted(("Saturday", "Sunday")):
        on = "weekends"
    else:
        on = ", ".join(d[:3] for d in DAYS if d in days)
    scope = ("every week" if rule.get("scope") == "every"
             else f"the week of {mdy(rule.get('week_start'))} only")
    return f"at least {rule['min_people']} {rule['role']} at {when}, {on} · {scope}"


def _row(r):
    d = dict(r)
    d["dayparts"] = [p for p in json.loads(d.get("dayparts") or "[]") if p in DAYPARTS] or list(DAYPARTS)
    d["days"] = [x for x in json.loads(d["days"]) if x in DAYS] if d.get("days") else None
    d["words"] = words_for(d)
    return d


def note_rules(restaurant_id, week_start=None, include_past=False, db_path=DB_PATH) -> list:
    """The active rules: every standing one, and the week-only ones for
    `week_start`'s week (any date in it) — or, without one, every week-only
    rule whose week has not ended (all of them with `include_past`)."""
    conn = get_conn(db_path)
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM schedule_note_rules WHERE restaurant_id=? AND removed_at IS NULL ORDER BY id",
            (restaurant_id,)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
    if week_start:
        mon = _monday(week_start).isoformat()
        return [r for r in rows if r["scope"] == "every" or r.get("week_start") == mon]
    if include_past:
        return rows
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        today = date.today()
    this_monday = (today - timedelta(days=today.weekday())).isoformat()
    return [r for r in rows if r["scope"] == "every" or (r.get("week_start") or "") >= this_monday]


def add_rule(restaurant_id, role, min_people, dayparts, days=None, scope="every", week_start=None,
             source_text=None, user=None, db_path=DB_PATH) -> dict:
    """Store one confirmed rule. Raises ValueError with the owner's words."""
    roles = restaurant_roles(restaurant_id, db_path=db_path)
    match = next((r for r in roles if r.lower() == str(role or "").strip().lower()), None)
    if not match:
        raise ValueError("Pick one of your roles for this rule.")
    try:
        n = int(min_people)
        whole = float(min_people) == n
    except (TypeError, ValueError):
        raise ValueError("The number of people must be a whole number.")
    if not whole:
        raise ValueError("The number of people must be a whole number.")
    if isinstance(min_people, bool) or not 1 <= n <= MIN_MAX:
        raise ValueError(f"The number of people must be from 1 to {MIN_MAX}.")
    parts = [p for p in DAYPARTS if p in (dayparts or [])]
    if not parts:
        raise ValueError("Pick lunch, dinner or both.")
    day_list = [d for d in DAYS if d in (days or [])] or None
    if scope not in ("every", "week"):
        raise ValueError("A rule is for every week or one week.")
    mon = None
    if scope == "week":
        import schedule_engine
        raw, err = schedule_engine.check_week_start(restaurant_id, week_start or "")
        if err:
            raise ValueError(err)
        if raw:
            mon = _monday(raw)
        else:
            # No week named: next week, the one Generate drafts by default.
            from time_utils import restaurant_now_by_id
            today = restaurant_now_by_id(restaurant_id, naive=True).date()
            mon = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        mon = mon.isoformat()
    who = None
    if user:
        who = user.get("email") or user.get("username") or (f"user {user.get('id')}" if user.get("id") else None)
    # The same rule twice (a double click, a second save) is the one rule
    # already in force — never a duplicate the sentence can't account for.
    for r in note_rules(restaurant_id, include_past=True, db_path=db_path):
        if (r["scope"], r.get("week_start"), r["role"].lower(), r["min_people"], r["dayparts"], r["days"]) == \
                (scope, mon, match.lower(), n, parts, day_list):
            return dict(r, existing=True)
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO schedule_note_rules (restaurant_id, scope, week_start, role, min_people, dayparts, days, "
            "source_text, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, scope, mon, match, n, json.dumps(parts), json.dumps(day_list) if day_list else None,
             " ".join(str(source_text or "").split())[:SOURCE_MAX] or None, who))
        conn.commit()
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM schedule_note_rules WHERE id=?", (rid,)).fetchone()
    finally:
        conn.close()
    return _row(row)


def remove_rule(restaurant_id, rule_id, user=None, db_path=DB_PATH):
    """The removed rule (its words), or None when it was already gone."""
    who = ((user or {}).get("email") or (user or {}).get("username")) if user else None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM schedule_note_rules WHERE id=? AND restaurant_id=? AND removed_at IS NULL",
                           (int(rule_id), restaurant_id)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE schedule_note_rules SET removed_at=datetime('now'), removed_by=? WHERE id=?",
                     (who, int(rule_id)))
        conn.commit()
        return _row(row)
    finally:
        conn.close()


def _trades(c, day, part) -> bool:
    """Whether the restaurant serves `part` on `day` by its own hours: a
    restaurant opening at 3pm or later has no lunch, one closing by 3pm no
    dinner (the schedule's own 3pm split). Unknown hours trade both."""
    try:
        from schedule_engine import _DAYPART_SPLIT
        from schedule_rules import parse_minutes
        o = parse_minutes((getattr(c, "open_times", None) or {}).get(day, "") or "")
        cl = parse_minutes((getattr(c, "close_times", None) or {}).get(day, "") or "")
    except Exception:
        return True
    if part == "morning" and o is not None and o >= _DAYPART_SPLIT:
        return False
    if part == "night" and cl is not None and o is not None and o < cl <= _DAYPART_SPLIT:
        return False
    return True


def apply_note_rules(c, restaurant_id, db_path=DB_PATH):
    """Raise `c.role_floors` to every rule in force for the week being built
    (c.week_dates), naming the note each one came from in
    c.rule_floor_sources. A daypart the restaurant doesn't serve that day is
    skipped (a "never cut the host" at a dinner-only place is a dinner
    floor). A rule whose role nobody on the roster holds any more is named
    in c.owner_rules_unchecked instead of becoming a floor nobody can fill.
    A one-week rule holds only when every date being built is in its week
    (the publish gate reads a two-week range; floors are by weekday).
    Never raises."""
    try:
        if not c.week_dates:
            return
        import schedule_rules
        roles = {r.lower() for r in restaurant_roles(restaurant_id, db_path=db_path)}
        mondays = {_monday(d).isoformat() for d in c.week_dates}
        for r in note_rules(restaurant_id, week_start=c.week_dates[0], db_path=db_path):
            if r["scope"] == "week" and mondays != {r.get("week_start")}:
                continue
            if r["role"].lower() not in roles:
                c.owner_rules_unchecked.append(
                    f"{r.get('source_text') or r['words']} — nobody on the roster is a {r['role']} now")
                continue
            key = next((x for x in c.role_floors if x.strip().lower() == r["role"].lower()), r["role"])
            spec = c.role_floors.setdefault(key, {"morning": 0, "night": 0, "days": {}})
            # Named in a breach as 'your rule: "keep two cooks on Friday lunch"'.
            src = r.get("source_text") or r["words"]
            for day in (r["days"] or DAYS):
                for part in r["dayparts"]:
                    if not _trades(c, day, part):
                        continue
                    if schedule_rules.floor_for(c.role_floors, key, day, part) < r["min_people"]:
                        spec.setdefault("days", {}).setdefault(day, {})[part] = r["min_people"]
                        c.rule_floor_sources[(key.strip().lower(), day, part)] = src
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="schedule_note_rules", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
