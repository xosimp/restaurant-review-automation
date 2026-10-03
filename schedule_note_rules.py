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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_note_rules)").fetchall()}
        if "more_sources" not in cols:
            # The other sentences the same rule was confirmed from (re-audit
            # 10/2/26: a second sentence with the same rule showed as unmade).
            conn.execute("ALTER TABLE schedule_note_rules ADD COLUMN more_sources TEXT")
        # A rule about WHEN a role starts or ends, not how many (schedule
        # audit 10/3/26 L-33: "make it a rule" for a start the manager keeps
        # setting): kind 'floor' (the minimum above, every row before this),
        # 'start' or 'end' with at_time ("4:30pm"); min_people is 0 for a
        # time rule.
        for _col, _decl in (("kind", "TEXT NOT NULL DEFAULT 'floor'"), ("at_time", "TEXT")):
            if _col not in cols:
                conn.execute(f"ALTER TABLE schedule_note_rules ADD COLUMN {_col} {_decl}")
        conn.commit()
    finally:
        conn.close()


# ── reading the notes ───────────────────────────────────────────────────────
#
# An ALLOWLIST, not a blocklist (blind re-audit, 10/2/26): a list of words
# that make a sentence "not a rule" always misses the next phrasing ("4th
# of July", "tonight", "2 trained servers", "but Saturday is 3"). A sentence
# is offered as a rule only when EVERY word in it is accounted for — one
# count, one of the restaurant's roles, days, lunch or dinner, scope words
# and a short list of words that say "at least" — and anything left over
# makes it "not checked", naming the words it couldn't hold.

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10, "eleven": 11, "twelve": 12, "couple": 2, "pair": 2}
_STAFF_WORDS = re.compile(r"\b(staff\w*|shifts?|cover(?:s|age)?|clos(?:e|es|er|ers|ing)|open(?:er|ers|ing)?|"
                          r"cut|send home|on the floor|people|person|crew|team|schedul\w*|hours)\b")
_MAX_RX = re.compile(r"\b(no more than|at most|maximum|max|cap|capped|never more than|up to|limit|only \d+|"
                     r"only (?:one|two|three|four|five|six|a))\b")
_WEEK_RX = re.compile(r"\b(this week|next week|this coming week|the week of|for the week)\b")
# First names that are also everyday words, never read as a person.
_COMMON_WORDS = {"will", "may", "june", "april", "august", "rose", "mark", "bill", "pat", "sue", "art", "max",
                 "grant", "chase", "hope", "joy", "summer", "dawn", "faith", "frank", "jack", "don", "ray",
                 "per", "erin", "rich", "sunny", "ken", "drew", "dean", "jean", "lane", "page", "reid", "wade"}
# The words a plain minimum may be made of, besides its count, role, days
# and dayparts.
_FILLER = {"keep", "keeps", "kept", "need", "needs", "needed", "have", "has", "always", "least", "minimum",
           "min", "must", "schedule", "scheduled", "staff", "staffed", "put", "want", "wants", "ensure", "require",
           "requires", "required", "make", "sure", "run", "with", "on", "the", "at", "every", "each", "all", "in",
           "for", "of", "during", "shift", "shifts", "our", "we", "please", "and", "be", "is", "are", "there",
           "should", "get", "working", "work", "a", "an", "only", "times", "time", "both", "us", "i", "also",
           "floor", "people", "person", "persons", "ppl", "atleast", "x", "will", "would", "can", "could",
           "to", "lets", "let", "ll", "s", "d", "going", "gonna", "do", "does", "needs", "keeping", "having",
           "duty", "covered", "cover", "scheduled"}
# Role words people use for the roster's names (blind re-audit #8).
_ROLE_SYNONYMS = {"hostess": "host", "hostesses": "host", "busboy": "busser", "busboys": "busser",
                  "busperson": "busser", "buspeople": "busser", "mgr": "manager", "mgrs": "manager",
                  "mngr": "manager", "expediter": "expo", "expeditor": "expo", "expediters": "expo",
                  "linecook": "line cook", "linecooks": "line cook", "dishwashers": "dishwasher",
                  "dish": "dishwasher", "dishes": "dishwasher", "bartenders": "bartender", "barkeep": "bartender",
                  "waiter": "server", "waiters": "server", "waitress": "server", "waitresses": "server",
                  "chef": "cook", "chefs": "cook"}
_DAYPART_TOKENS = {"lunch": "morning", "lunches": "morning", "brunch": "morning", "breakfast": "morning",
                   "morning": "morning", "mornings": "morning", "am": "morning", "daytime": "morning",
                   "open": "morning", "opening": "morning", "opens": "morning",
                   "dinner": "night", "dinners": "night", "night": "night", "nights": "night", "nite": "night",
                   "nites": "night", "evening": "night", "evenings": "night", "eve": "night", "pm": "night",
                   "close": "night", "closing": "night", "closes": "night", "weeknight": "night",
                   "weeknights": "night"}
_DAY_ABBR = {"mon": "Monday", "monday": "Monday", "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
             "wed": "Wednesday", "weds": "Wednesday", "wednesday": "Wednesday", "thu": "Thursday",
             "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday", "fri": "Friday", "friday": "Friday",
             "sat": "Saturday", "saturday": "Saturday", "sun": "Sunday", "sunday": "Sunday"}
_DAY_GROUPS = {"weekend": ("Saturday", "Sunday"), "weekends": ("Saturday", "Sunday"),
               "weekday": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
               "weekdays": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"),
               "weeknight": ("Monday", "Tuesday", "Wednesday", "Thursday"),
               "weeknights": ("Monday", "Tuesday", "Wednesday", "Thursday")}
_EVERY_DAY = {"day", "days", "daily", "week", "everyday"}
_RANGE_WORDS = {"through", "thru", "to", "till", "until", "-"}
# Shorthand for runs of days ("M-F", "MWF", "T/Th").
_DAY_SHORT = [(r"\bm\s*-\s*f\b", "monday through friday"), (r"\bm\s*-\s*th\b", "monday through thursday"),
              (r"\bsu\s*-\s*th\b", "sunday through thursday"), (r"\bmwf\b", "monday wednesday friday"),
              (r"\bt\s*/\s*th\b|\btu\s*/?\s*th\b|\btth\b", "tuesday thursday"),
              (r"\bf\s*/\s*sa\b|\bf\s*-\s*sa\b", "friday saturday"), (r"\bsa\s*/\s*su\b|\bsa\s*-\s*su\b", "saturday sunday")]
_ABBREV_DOT = ("mon", "tue", "tues", "wed", "weds", "thu", "thur", "thurs", "fri", "sat", "sun", "st", "min", "max",
               "vs", "a.m", "p.m", "approx", "dr", "mr", "mrs", "ms", "no")


def _sentences(text):
    """Lines, then sentences — never split after a day abbreviation, "St.",
    "a.m." or a lone initial ("Keep 2 bartenders Fri. Sat. and Sun." is one)."""
    out = []
    for line in str(text or "").replace("\r", "\n").split("\n"):
        line = line.strip(" -•*\t")
        if not line:
            continue
        parts, cur = [], ""
        for piece in re.split(r"(;|[.!?]\s+)", line):
            if piece == ";":
                parts.append(cur); cur = ""
                continue
            if piece and re.fullmatch(r"[.!?]\s+", piece):
                last = re.findall(r"[A-Za-z.]+$", cur)
                word = last[0].lower().rstrip(".") if last else ""
                if word in _ABBREV_DOT or len(word) <= 1:
                    cur += piece
                    continue
                parts.append(cur + piece.strip()); cur = ""
                continue
            cur += piece
        parts.append(cur)
        for p in parts:
            p = p.strip()
            if len(p.strip(" .!?;")) >= 3:
                out.append(p)
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


def _normalise(text, roles=()):
    """Lower case, the minimum shapes rewritten to "at least N", synonyms and
    shorthand folded. Returns (low, notes) — notes are words that decide the
    reading before the allowlist runs."""
    low = " ".join(str(text or "").lower().replace("’", "'").replace("‘", "'").split())
    low = low.replace("a.m.", " am ").replace("p.m.", " pm ").replace("&", " and ")
    low = re.sub(r"(\d)\s*(am|pm)\b", r"\1 \2", low)
    for rx, rep in _DAY_SHORT:
        low = re.sub(rx, rep, low)
    low = re.sub(r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)\.", r"\1", low)
    low = re.sub(r"\b(\d+)\s*\+", r"at least \1", low)
    low = re.sub(r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+or more\b", r"at least \1", low)
    low = re.sub(r"\b(?:no|not|never) (?:fewer|less) th[ae]n\b|\bnever (?:go |drop |run |be )?(?:below|under)\b|"
                 r"\bnever (?:let (?:it|us) )?(?:drop|go|get) below\b|\bnever run with (?:fewer|less) th[ae]n\b|"
                 r"\bnever cut below\b|\bno less than\b|\bnot below\b|\bnever under\b|\bbare minimum of\b|"
                 r"\bminimum of\b", " at least ", low)
    low = re.sub(r"\b(?:never|don't|dont|do not|please don't|please do not)\s+(?:cut|send home|drop|remove|schedule "
                 r"without|go without|run without|skip)\s+(?:the|our|a|an|any)?\s*", " at least 1 ", low)
    own = {_singular(str(r).lower().split()[-1]) for r in roles or () if str(r or "").strip()} | \
        {str(r).lower() for r in roles or ()}
    for w, rep in _ROLE_SYNONYMS.items():
        if _singular(w) in own or w in own:
            continue                     # the roster's own word ("Dish", "Chef") stays itself
        low = re.sub(r"\b" + re.escape(w) + r"\b", rep, low)
    low = re.sub(r"\b(\w+)'s\b", r"\1", low)
    return " ".join(low.split())


def _match_roles(low, roles):
    """(role, choices, the words that named it): an exact role name first
    (longest), else a role family the job codes share ("servers" where the
    codes are "Server AM" and "Server PM" — schedule audit 10/3/26 D-14: it
    named no role, so "keep two servers at dinner" was never a rule; the
    floor is held on the code for that half of the day), else the roles
    whose last word the sentence names ("cooks" → Line Cook and Prep Cook:
    two choices, the owner picks)."""
    clean = sorted({str(r).strip() for r in roles or () if str(r or "").strip()}, key=len, reverse=True)
    for r in clean:
        if re.search(r"\b" + re.escape(r.lower()) + r"(?:s|es)?\b", low):
            return r, [r], r.lower()
    from shift_quality import role_family
    from schedule_rules import _family_name
    families = {}
    for r in clean:
        families.setdefault(role_family(r), []).append(r)
    for fam in sorted((f for f, codes in families.items() if f and (len(codes) > 1 or codes[0].lower() != f)),
                      key=len, reverse=True):
        if re.search(r"\b" + re.escape(fam) + r"(?:s|es)?\b", low):
            name = _family_name(fam, families[fam])
            return name, [name], fam
    words = {_singular(w) for w in re.findall(r"[a-z]+", low)}
    hits = [r for r in clean if _singular(r.lower().split()[-1]) in words]
    if not hits:
        return None, [], None
    word = _singular(hits[0].lower().split()[-1])
    return (hits[0] if len(hits) == 1 else None), hits, word


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


def _day_of(word):
    return _DAY_ABBR.get(word.lower()) or _DAY_ABBR.get(word.lower().rstrip("s"))


def _days(tokens):
    """(days or None for every day, why-not or None) from the day tokens:
    single days, ranges ("friday through sunday", wrapping "sunday to
    thursday"), weekends / weekdays / weeknights, and "every day except
    Monday" — the complement only when the sentence names no other day
    (blind re-audit #1)."""
    days, i, except_days, in_except = [], 0, [], False
    while i < len(tokens):
        t = tokens[i]
        if t in ("except", "besides", "excluding"):
            in_except = True
            i += 1
            continue
        d = _day_of(t)
        if d and i + 2 < len(tokens) and tokens[i + 1] in _RANGE_WORDS and _day_of(tokens[i + 2]):
            a, b = DAYS.index(d), DAYS.index(_day_of(tokens[i + 2]))
            run, k = [], a
            while True:
                run.append(DAYS[k])
                if k == b:
                    break
                k = (k + 1) % 7
            (except_days if in_except else days).extend(run)
            i += 3
            continue
        if d:
            (except_days if in_except else days).append(d)
        elif t in _DAY_GROUPS:
            (except_days if in_except else days).extend(_DAY_GROUPS[t])
        i += 1
    if except_days:
        if days:
            return None, "It names days to have and days to leave out in one sentence — make one rule per set of days."
        return [d for d in DAYS if d not in set(except_days)], None
    out = [d for d in DAYS if d in set(days)]
    return (out or None), None


def _person(text, low, names):
    """A roster name the sentence is about: a multi-word name in full, or a
    first name (any case) that isn't an everyday word."""
    for nm in sorted({str(x).strip() for x in names or () if str(x or "").strip()}, key=len, reverse=True):
        parts = nm.split()
        first = parts[0].lower()
        if len(parts) > 1 and re.search(r"\b" + re.escape(nm.lower()) + r"\b", low):
            return nm
        if first in _COMMON_WORDS or len(first) < 3:
            continue
        if re.search(r"\b" + re.escape(first) + r"\b", low):
            return nm
    return None


def _unchecked(out, why):
    return dict(out, kind="unchecked", why=why)


def read_sentence(text, roles, names=()) -> dict:
    """{"text", "kind": "rule" | "unchecked" | "person" | "guidance", "why",
    "rule"?} for one sentence of the notes. A rule carries {"role" (None
    when two roles fit), "role_choices", "min", "dayparts", "days", "scope"
    ("every" | "week")} — the owner's to confirm or change. A rule only when
    every word is accounted for (see above); otherwise "not checked" with
    why — never a rule that means something else."""
    out = {"text": " ".join(str(text).split())}
    low = _normalise(text, roles)
    role, choices, word = _match_roles(low, roles)
    person = _person(text, low, names)
    count_words = re.findall(r"\b(\d+(?:\.\d+)?|" + "|".join(_NUM) + r")\b", low)
    if person and not (choices and count_words):
        return dict(out, kind="person", person=person,
                    why=f"It's about {person} — put it in their scheduling notes, where it's a hard rule with a date.")
    staffing = bool(choices) or bool(_STAFF_WORDS.search(low)) or bool(count_words)
    if not choices:
        if staffing:
            return _unchecked(out, "It doesn't name one of your roles as your roster spells it, so no rule can be "
                                   "made from it — pick the role yourself.")
        return dict(out, kind="guidance", why="Not about who works when — the draft reads it; nothing checks it.")
    if _MAX_RX.search(low):
        return _unchecked(out, "A most-people limit — Cavnar AI can hold a minimum, not a maximum yet.")
    if re.search(r"\b(send|sent)\b.*\bhome\b|\bgo home\b|\bcut\b|\blet\b.*\bgo\b|\bleave early\b|\bcut back\b", low):
        return _unchecked(out, "It's about cutting or sending people home — a rule holds who must be on, not who goes.")
    if re.search(r"(n't\b|\b(?:no|not|never|without|zero|none|dont|cant|cannot|wont|shouldnt|too many|too much|"
                 r"overkill|enough|plenty)\b)", low):
        return _unchecked(out, "It says what not to do — a rule can only say how many must be on.")
    # "We have 6 servers" states a fact, not a must (re-audit, 10/1/26).
    if re.search(r"\b(?:we|they|i|there)\s+(?:have|has|had|got|are|were)\b", low) and \
            not re.search(r"\b(always|must|should|need|needs|want|wants|keep|at least|minimum|min|require|sure|"
                          r"ensure)\b", low):
        return _unchecked(out, "It reads as a fact, not a must — say \u201ckeep at least …\u201d to make it a rule.")
    if re.search(r"\b\d+\.\d+\b", low):
        return _unchecked(out, "A rule counts whole people.")
    if re.search(r"\b(\d+|" + "|".join(_NUM) + r")\s*(?:-|–|to|or)\s*(\d+|" + "|".join(_NUM) + r")\b", low):
        return _unchecked(out, "It gives a range — say the least number you want on, like “at least 2”.")
    if re.search(r"\b(few|several|some|more|extra|another|additional|fewer|less|double|enough)\b", low):
        return _unchecked(out, "It doesn't say a number of people — say the least you want on, like "
                               "“at least 3”.")
    if len(_role_mentions(low, roles)) > 1:
        return _unchecked(out, "It names more than one role — make one rule for each.")
    nums = [w for w in count_words]
    if len(nums) > 1:
        return _unchecked(out, "It has more than one number in it (a second count, a time or a date) — make one "
                               "rule per count, with no times.")
    # The count: the one number, or "a"/"an" right before the role ("keep a host").
    n = None
    if nums:
        n = int(nums[0]) if nums[0].isdigit() else _NUM.get(nums[0])
    elif re.search(r"\b(?:a|an)\s+(?:\w+\s+)?" + re.escape(word) + r"(?:s|es)?\b", low) or \
            re.search(r"\bat least 1\b", low) or \
            (re.search(r"\bthe\s+" + re.escape(word) + r"\b", low) and
             re.search(r"\b(always|keep|must|need|needs|should|make sure|ensure)\b", low)):
        n = 1          # "keep a host", "the host should always be on"
    if not n:
        return _unchecked(out, "No number of people Cavnar AI can hold the draft to.")
    if n > MIN_MAX:
        return _unchecked(out, f"More than {MIN_MAX} — the most one rule holds is {MIN_MAX}; make it two rules or "
                               "check the draft yourself.")
    # Every other word must be one Cavnar AI can hold.
    rest = low
    rest = re.sub(r"\b" + re.escape((role or word).lower()) + r"(?:s|es)?\b", " ", rest)
    for c in choices:
        rest = re.sub(r"\b" + re.escape(c.lower()) + r"(?:s|es)?\b", " ", rest)
    if word:
        rest = re.sub(r"\b" + re.escape(word) + r"(?:s|es)?\b", " ", rest)
    rest = re.sub(r"\bduring the day\b|\bin the day\b", " am ", rest)
    rest = re.sub(r"\bat least\b|\bthis week\b|\bnext week\b|\bthis coming week\b|\bfor the week\b|"
                  r"\bat all times\b|\ball day\b|\bday shift\b|\bnight shift\b|\bpm shift\b|\bam shift\b|"
                  r"\bevery day\b|\ball week\b|\beach day\b", " ", rest)
    rest = re.sub(r"\bother than\b|\bapart from\b|\bbut not\b", " except ", rest)
    tokens = re.findall(r"[a-z]+|\d+|-", rest)
    days, days_why = _days(tokens)
    if days_why:
        return _unchecked(out, days_why)
    leftover = []
    for t in tokens:
        if t in _FILLER or t in _NUM or t.isdigit() or t in _DAYPART_TOKENS or t in _DAY_GROUPS or \
                t in _EVERY_DAY or t in _RANGE_WORDS or t in ("except", "besides", "excluding") \
                or _day_of(t):
            continue
        leftover.append(t)
    if leftover:
        said = ", ".join(f"“{w}”" for w in dict.fromkeys(leftover))
        return _unchecked(out, f"Cavnar AI can't hold the part about {said} — a rule is a number of one role on "
                               "chosen days at lunch or dinner. Make the rule without it, or check the draft yourself.")
    parts = sorted({_DAYPART_TOKENS[t] for t in tokens if t in _DAYPART_TOKENS}, key=DAYPARTS.index)
    if re.search(r"\b(day shift|am shift)\b", low):
        parts = sorted(set(parts) | {"morning"}, key=DAYPARTS.index)
    if re.search(r"\b(night shift|pm shift)\b", low):
        parts = sorted(set(parts) | {"night"}, key=DAYPARTS.index)
    rule = {"role": role, "role_choices": choices, "min": int(n), "dayparts": parts or ["morning", "night"],
            "days": days, "scope": "week" if _WEEK_RX.search(low) else "every"}
    why = None
    if role is None:
        why = f"More than one of your roles fits “{word}” — pick which."
    elif re.search(r"\bweeknights?\b", low):
        why = "“Weeknights” is read as Monday to Thursday — change the days if you mean otherwise."
    elif not parts:
        why = "No lunch or dinner named — read as both; change it if you mean one."
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
        for src in [r.get("source_text")] + list(r.get("more_sources") or []):
            active.setdefault(_key(src), r)
    out = []
    for s in _sentences(text):
        item = read_sentence(s, roles, names)
        hit = active.get(_key(s))
        if hit:
            item["rule_id"], item["enforced"] = hit["id"], hit["words"]
            if hit.get("stale"):
                item["stale"] = hit["stale"]
        out.append(item)
    return out


def _key(text):
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _held(role, held) -> bool:
    """Whether somebody on the roster holds `role` — the job code itself, or
    a code of the same role family ("Server" for "Server PM", D-14)."""
    from shift_quality import role_family
    return str(role or "").lower() in held or role_family(role) in {role_family(x) for x in held}


def roster_roles(restaurant_id, db_path=DB_PATH) -> set:
    """Lower-cased roles somebody on the roster holds now. None when the
    roster can't be read (nothing is called stale then)."""
    try:
        import staff_settings
        return {str(e.get("role") or "").strip().lower() for e in staff_settings.roster(restaurant_id, db_path=db_path)
                if e.get("role") and e.get("active") is not False}
    except Exception:
        return None


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


TIME_KINDS = ("start", "end")


def words_for(rule) -> str:
    """"at least 2 Line Cook at lunch and dinner, Fridays · every week" —
    or, for a time rule, "Server at dinner start at 4:30pm, Fri · every week"."""
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
    if (rule.get("kind") or "floor") in TIME_KINDS:
        return f"{rule['role']} at {when} {rule['kind']} at {rule.get('at_time')}, {on} · {scope}"
    return f"at least {rule['min_people']} {rule['role']} at {when}, {on} · {scope}"


def _row(r):
    d = dict(r)
    d["kind"] = d.get("kind") or "floor"
    d["dayparts"] = [p for p in json.loads(d.get("dayparts") or "[]") if p in DAYPARTS] or list(DAYPARTS)
    d["days"] = [x for x in json.loads(d["days"]) if x in DAYS] if d.get("days") else None
    try:
        d["more_sources"] = json.loads(d.get("more_sources") or "[]") or []
    except (TypeError, ValueError):
        d["more_sources"] = []
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
    held = roster_roles(restaurant_id, db_path=db_path)
    if held is not None:
        for r in rows:
            if not _held(r["role"], held):
                r["stale"] = f"Not held: nobody on the roster is a {r['role']} now."
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
    if not match and str(role or "").strip():
        # A role family the job codes share ("Server" for Server AM / PM —
        # D-14): held on the code for each half of the day.
        from shift_quality import role_family
        from schedule_rules import _family_name
        fam = role_family(role)
        codes = [r for r in roles if role_family(r) == fam]
        match = _family_name(fam, codes) if codes else None
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
    bad = [str(d) for d in (days or []) if str(d) not in DAYS]
    if bad:
        raise ValueError(f"{bad[0]} isn't a day — use Monday to Sunday.")
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
        if r.get("kind", "floor") != "floor":
            continue
        if (r["scope"], r.get("week_start"), r["role"].lower(), r["min_people"], r["dayparts"], r["days"]) == \
                (scope, mon, match.lower(), n, parts, day_list):
            src = " ".join(str(source_text or "").split())[:SOURCE_MAX]
            if src and _key(src) not in {_key(x) for x in [r.get("source_text")] + r["more_sources"]}:
                conn = get_conn(db_path)
                try:
                    conn.execute("UPDATE schedule_note_rules SET more_sources=? WHERE id=?",
                                 (json.dumps(r["more_sources"] + [src]), r["id"]))
                    conn.commit()
                finally:
                    conn.close()
                r = dict(r, more_sources=r["more_sources"] + [src])
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


def add_time_rule(restaurant_id, role, kind, at_time, daypart, days=None, source_text=None, user=None,
                  db_path=DB_PATH) -> dict:
    """Store one confirmed START or END rule for a role on a daypart (every
    week): "Servers start Friday dinner at 4:30pm". From then on the draft
    is built to it (schedule_rules.apply_role_times), the model is told it
    (schedule_rules.prompt_block) and a row off it is a `role_time` breach
    that names the rule (schedule audit 10/3/26 L-33). Raises ValueError
    with the owner's words."""
    if kind not in TIME_KINDS:
        raise ValueError("A time rule is a start or an end.")
    roles = restaurant_roles(restaurant_id, db_path=db_path)
    match = next((r for r in roles if r.lower() == str(role or "").strip().lower()), None)
    if not match:
        raise ValueError("Pick one of your roles for this rule.")
    if daypart not in DAYPARTS:
        raise ValueError("Pick lunch or dinner.")
    from schedule_rules import parse_minutes, _fmt_minutes
    m = parse_minutes(str(at_time or ""))
    if m is None:
        raise ValueError("That time isn't one we can read — try 4:30pm.")
    at = _fmt_minutes(m)
    bad = [str(d) for d in (days or []) if str(d) not in DAYS]
    if bad:
        raise ValueError(f"{bad[0]} isn't a day — use Monday to Sunday.")
    day_list = [d for d in DAYS if d in (days or [])] or None
    who = None
    if user:
        who = user.get("email") or user.get("username") or (f"user {user.get('id')}" if user.get("id") else None)
    have = note_rules(restaurant_id, include_past=True, db_path=db_path)
    for r in have:
        if (r.get("kind"), r["role"].lower(), r["dayparts"], r["days"], r.get("at_time")) == \
                (kind, match.lower(), [daypart], day_list, at) and r["scope"] == "every":
            return dict(r, existing=True)
    conn = get_conn(db_path)
    try:
        # One time per role, edge, daypart and day: a newer rule replaces the
        # one it contradicts (the owner changed their mind), never sits beside it.
        for r in have:
            if r.get("kind") == kind and r["role"].lower() == match.lower() and r["dayparts"] == [daypart] \
                    and r["days"] == day_list and r["scope"] == "every":
                conn.execute("UPDATE schedule_note_rules SET removed_at=datetime('now'), removed_by=? WHERE id=?",
                             (who, r["id"]))
        cur = conn.execute(
            "INSERT INTO schedule_note_rules (restaurant_id, scope, week_start, role, min_people, dayparts, days, "
            "source_text, created_by, kind, at_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, "every", None, match, 0, json.dumps([daypart]),
             json.dumps(day_list) if day_list else None,
             " ".join(str(source_text or "").split())[:SOURCE_MAX] or None, who, kind, at))
        conn.commit()
        row = conn.execute("SELECT * FROM schedule_note_rules WHERE id=?", (cur.lastrowid,)).fetchone()
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
    # Closing between 10am and 3pm is a lunch-only day (a 1am close is a
    # late night, never this).
    if part == "night" and cl is not None and 10 * 60 <= cl <= _DAYPART_SPLIT and (o is None or o < cl):
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
        held = roster_roles(restaurant_id, db_path=db_path)
        roles = held if held is not None else {r.lower() for r in restaurant_roles(restaurant_id, db_path=db_path)}
        mondays = {_monday(d).isoformat() for d in c.week_dates}
        for r in note_rules(restaurant_id, week_start=c.week_dates[0], db_path=db_path):
            if r["scope"] == "week" and mondays != {r.get("week_start")}:
                continue
            if not _held(r["role"], roles):
                c.owner_rules_unchecked.append(
                    f"{r.get('source_text') or r['words']} — nobody on the roster is a {r['role']} now")
                continue
            if r.get("kind") in TIME_KINDS:
                # A start or end the owner made a rule (L-33): the draft is
                # built to it and checked against it (Constraints.role_times).
                from schedule_rules import parse_minutes
                at = parse_minutes(r.get("at_time") or "")
                if at is None:
                    continue
                times = getattr(c, "role_times", None)
                if times is None:
                    continue
                for day in (r["days"] or DAYS):
                    for part in r["dayparts"]:
                        if not _trades(c, day, part):
                            continue
                        spec = times.setdefault((r["role"].strip().lower(), day, part), {})
                        spec[r["kind"]] = at
                        spec.setdefault("source", {})[r["kind"]] = r.get("source_text") or r["words"]
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
