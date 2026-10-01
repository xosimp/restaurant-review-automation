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
_MAX_RX = re.compile(r"\b(no more than|at most|maximum|max of|cap|never more than|only)\b")
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


def _days(low):
    import schedule_rules
    d = schedule_rules._rule_days(low)
    return list(d) if d else None


def _dayparts(low):
    import schedule_rules
    has_m = any(re.search(r"\b" + w + r"\b", low) for w, p in schedule_rules._RULE_DAYPARTS.items() if p == "morning")
    has_n = any(re.search(r"\b" + w + r"\b", low) for w, p in schedule_rules._RULE_DAYPARTS.items() if p == "night")
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


def read_sentence(text, roles, names=()) -> dict:
    """{"text", "kind": "rule" | "unchecked" | "guidance", "why", "rule"?}
    for one sentence of the notes. A rule carries {"role" (None when two
    roles fit), "role_choices", "min", "dayparts", "days", "scope"
    ("every" | "week")} — the owner's to confirm or change."""
    import schedule_rules
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    role, choices, word = _match_roles(low, roles)
    staffing = bool(choices) or bool(_STAFF_WORDS.search(low))
    out = {"text": " ".join(str(text).split())}
    # About one person ("Marcus only closes on weekends"): that belongs in
    # their dated scheduling notes, which the draft holds as a hard rule.
    # The full name, or the first name capitalised mid-sentence ("Will"
    # opening a sentence is a verb as often as a name).
    raw = " ".join(str(text or "").split())
    person = next((nm for nm in sorted({str(x).strip() for x in names or () if str(x or "").strip()},
                                       key=len, reverse=True)
                   if re.search(r"\b" + re.escape(nm.lower()) + r"\b", low)
                   or re.search(r"(?<=\S)\s+" + re.escape(nm.split()[0].capitalize()) + r"\b", raw)
                   or (re.match(re.escape(nm.split()[0].capitalize()) + r"\b", raw)
                       and nm.split()[0].lower() not in _COMMON_WORDS)), None)
    if person:
        return dict(out, kind="person", person=person,
                    why=f"It's about {person} — put it in their scheduling notes, where it's a hard rule with a date.")
    if not choices:
        if staffing:
            return dict(out, kind="unchecked",
                        why="It doesn't name one of your roles, so no rule can be made from it.")
        return dict(out, kind="guidance", why="Not about who works when — the draft reads it; nothing checks it.")
    if _MAX_RX.search(low):
        return dict(out, kind="unchecked", why="A most-people limit — Cavnar AI can hold a minimum, not a maximum yet.")
    n = None
    parsed = schedule_rules.parse_owner_rule(text, choices)
    if parsed:
        n = parsed["min"]
    if n is None and not re.search(r"\b(no|not|without|never)\b", low):
        n = _number_before(low, word)
        if n is not None and _RELATIVE_RX.search(low):
            return dict(out, kind="unchecked",
                        why="It's relative to the usual (more, extra, fewer) — say the number you want on, "
                            "like “at least 3”.")
    if not n and _RELATIVE_RX.search(low):
        return dict(out, kind="unchecked",
                    why="It's relative to the usual (more, extra, fewer) — say the number you want on, "
                        "like \u201cat least 3\u201d.")
    if not n:
        return dict(out, kind="unchecked", why="No number of people Cavnar AI can hold the draft to.")
    rule = {"role": role, "role_choices": choices, "min": min(int(n), MIN_MAX),
            "dayparts": _dayparts(low) or ["morning", "night"], "days": _days(low),
            "scope": "week" if _WEEK_RX.search(low) else "every"}
    why = None
    if _EVENT_DAYS_RX.search(low) and not rule["days"]:
        why = "It names game or event days, which change week to week — pick the days for the week you're drafting."
        rule["scope"] = "week"
    elif role is None:
        why = f"More than one of your roles fits “{word}” — pick which."
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
    except (TypeError, ValueError):
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


def remove_rule(restaurant_id, rule_id, user=None, db_path=DB_PATH) -> bool:
    who = ((user or {}).get("email") or (user or {}).get("username")) if user else None
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE schedule_note_rules SET removed_at=datetime('now'), removed_by=? "
                           "WHERE id=? AND restaurant_id=? AND removed_at IS NULL", (who, int(rule_id), restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def apply_note_rules(c, restaurant_id, db_path=DB_PATH):
    """Raise `c.role_floors` to every rule in force for the week being built
    (c.week_dates), naming the note each one came from in
    c.rule_floor_sources. Never raises."""
    try:
        if not c.week_dates:
            return
        import schedule_rules
        for r in note_rules(restaurant_id, week_start=c.week_dates[0], db_path=db_path):
            key = next((x for x in c.role_floors if x.strip().lower() == r["role"].lower()), r["role"])
            spec = c.role_floors.setdefault(key, {"morning": 0, "night": 0, "days": {}})
            # Named in a breach as 'your rule: "keep two cooks on Friday lunch"'.
            src = r.get("source_text") or r["words"]
            for day in (r["days"] or DAYS):
                for part in r["dayparts"]:
                    if schedule_rules.floor_for(c.role_floors, key, day, part) < r["min_people"]:
                        spec.setdefault("days", {}).setdefault(day, {})[part] = r["min_people"]
                        c.rule_floor_sources[(key.strip().lower(), day, part)] = src
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="schedule_note_rules", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
