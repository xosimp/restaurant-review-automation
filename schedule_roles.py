"""
schedule_roles.py — the one reading of a job code and a person's name, for
every schedule module: a role's family ("Server AM" is a server), its half
of the day ("Barback PM" is the night), whether it is a training code, and
the key one person's name compares on.

Pure (no database, clock, model or network) and layer S0 of the schedule
modules (ARCHITECTURE_MANIFEST.md §3a): it imports nothing of the app, so
the scorer, the rules, the requirements table and the learned signals all
read roles here without importing each other. shift_quality re-exports
every name, so `shift_quality.role_family` keeps answering.
"""
import re
from functools import lru_cache


def _role_word_kept(w: str) -> bool:
    return w.lower() in ("am", "pm") or (len(w) > 1 and w.isupper())


def role_words(role, count: int = 1, default: str = "") -> str:
    """A role inside a sentence: ordinary words in lower case, AM/PM and
    any word the owner wrote in capitals kept that way — "Bartender AM" →
    "bartender AM", never "bartender am" (Will, 10/2/26). A count other
    than one pluralises the last ordinary word: "2 bartenders AM"."""
    words = str(role or "").split()
    if not words:
        return default
    out = [w.upper() if w.lower() in ("am", "pm") else (w if _role_word_kept(w) else w.lower()) for w in words]
    if count != 1:
        idx = [i for i, w in enumerate(out) if not _role_word_kept(w)]
        i = idx[-1] if idx else len(out) - 1
        w = out[i]
        out[i] = w + ("es" if w.endswith(("s", "sh", "ch", "x")) else "s")
    return " ".join(out)


# A role's family: the role with its daypart words taken off, so the job
# codes a POS splits by shift ("Server AM", "Server PM", "PM Bartender",
# "Host - Lunch") are one role to every rule that counts people in a role —
# requirements, floors, the section cap, leader rules, cross-training, the
# owner's "two servers Saturday". The restaurant's own map
# (restaurants.role_families_json, {role: family}) wins; a role the words
# would empty keeps its name.
_DAYPART_WORDS = frozenset({"am", "pm", "a.m.", "p.m.", "a.m", "p.m", "lunch", "dinner", "brunch", "breakfast", "day", "night",
                            "morning", "evening", "late", "overnight", "weekend", "weekday", "wknd", "open",
                            "opening", "opener", "close", "closing", "closer"})


def role_family(role, families=None) -> str:
    """'Server AM' → 'server'; 'Bartender - PM' → 'bartender'; the
    restaurant's map ({role lower: family}) first. Lower case."""
    low = " ".join(str(role or "").strip().lower().split())
    if not low:
        return ""
    if families:
        mapped = families.get(low)
        if mapped:
            return " ".join(str(mapped).strip().lower().split())
    return _family_words(low)


@lru_cache(maxsize=4096)
def _family_words(low: str) -> str:
    """The family a role name's own words give (role_family without the
    restaurant's map) — pure, so remembered: the scorer asks it for every
    person on every shift it scores."""
    cleaned = [w for w in role_tokens(low) if w not in _DAYPART_WORDS]
    return " ".join(cleaned) or low


# The one reading of the words in a job code, for its family and for its
# half of the day. role_family split on " / - _" while the daypart reader
# took only a last word of exactly "am"/"pm", so "Server-AM", "Server (PM)",
# "AM Server", "Lunch Server" and "Dinner Host" were servers and hosts with
# no half of the day: a leader rule or strength target on them bound lunch
# and dinner both.
_ROLE_SPLIT = str.maketrans({c: " " for c in "()[]/-_\u2013\u2014"})
_MORNING_WORDS = frozenset({"am", "a.m", "lunch", "brunch", "breakfast", "morning", "day", "open", "opening",
                            "opener"})
_NIGHT_WORDS = frozenset({"pm", "p.m", "dinner", "night", "evening", "late", "overnight", "close", "closing",
                          "closer"})


@lru_cache(maxsize=4096)
def _role_words_cached(low: str) -> tuple:
    return tuple(t for t in (w.strip(".,:;") for w in low.translate(_ROLE_SPLIT).split()) if t)


def role_tokens(role) -> list:
    """A job code's words, lower case, split on spaces and "( ) / - _" (and
    the like), trailing punctuation off: "Server (A.M.)" → ["server",
    "a.m"]."""
    return list(_role_words_cached(" ".join(str(role or "").lower().split())))


# A training job code ("Training", "Server Trainee", "Manager in Training",
# "MIT"): its shifts are never coverage. The one pattern the rules
# (schedule_rules.is_training_role) and the scorer's floor test read.
_TRAINING_ROLE = re.compile(r"\b(?:train(?:ing|ees?)|mits?)\b", re.I)


def is_training_role(role) -> bool:
    return bool(_TRAINING_ROLE.search(str(role or "")))


def is_training_row(row: dict, trainee: dict = None, families: dict = None) -> bool:
    """A training shift: a training job code, or the row of a trainee
    (`trainee`: their {target_role, from, until}) in the role they are
    learning while training lasts — Constraints.training_row and the
    scorer's floor test ask this one question (the score counted a training
    shift toward a floor the sweep did not)."""
    if is_training_role(row.get("role")):
        return True
    t = trainee or {}
    if not t or role_family(row.get("role"), families) != role_family(t.get("target_role"), families):
        return False
    d = row.get("date") or ""
    if not d:
        return True
    return (not t.get("from") or d >= t["from"]) and (not t.get("until") or d <= t["until"])


def role_daypart(role):
    """'morning' for a job code naming the day half ("Server AM",
    "Server-AM", "AM Server", "Lunch Bartender", "Server (A.M.)"), 'night'
    for the evening ("Barback PM", "PM Host", "Dinner Host", "Server_PM"),
    else None — also when it names both ("Lunch/Dinner Server"). The ONE
    reader: schedule_rules.role_daypart, models.leader_rule_daypart, the
    scorer's leader and strength checks, the solver, the requirements table
    and the floors all ask it."""
    words = set(role_tokens(role))
    morning, night = bool(words & _MORNING_WORDS), bool(words & _NIGHT_WORDS)
    if morning == night:
        return None
    return "morning" if morning else "night"


# A duty, not a half of the day: "Closer" and "Opener" are jobs of their
# own at any hour, so code_for_daypart never trades them for another code.
_DUTY_WORDS = frozenset({"open", "opening", "opener", "close", "closing", "closer"})


def _half_code(code):
    """The half of the day a job code is one half of a role's pair for, or
    None (no daypart word, or a duty code)."""
    part = role_daypart(code)
    if part is None or set(role_tokens(code)) & _DUTY_WORDS:
        return None
    return part


@lru_cache(maxsize=256)
def _fold_table(codes: tuple, families: tuple) -> dict:
    fam_map = dict(families) if families else None
    halves = {}
    for c in codes:
        part = _half_code(c)
        if part is not None:
            halves.setdefault((role_family(c, fam_map), part), []).append(c)
    out = {}
    for c in codes:
        part = _half_code(c)
        if part is None:
            continue
        other = "night" if part == "morning" else "morning"
        sib = sorted(halves.get((role_family(c, fam_map), other)) or (), key=lambda x: (x.casefold(), x))
        if sib:
            out[(c.casefold(), other)] = sib[0]
    return out


def fold_table(codes, families=None) -> dict:
    """{(code casefolded, part): the code it is counted as in that part} for
    every job code in `codes` that names the other half of the day and has
    a partner for `part` — code_for_daypart's table, built once per set of
    codes (the scorer reads it for every person on every shift)."""
    key = tuple(sorted({" ".join(str(c).split()) for c in codes or () if str(c or "").strip()}))
    fam = tuple(sorted((str(k), str(v)) for k, v in (families or {}).items()))
    return _fold_table(key, fam)


def code_for_daypart(role, part, codes=None, families=None, table=None) -> str:
    """The job code `role` counts as while it is on the floor for `part`
    ('morning' / 'night'). A code naming the OTHER half of the day ("Host
    AM" on at dinner) is its family's code for this half ("Host PM") when
    the restaurant has one among `codes` (or `table`, fold_table's); any
    other role is itself.

    A host on the floor at dinner is the dinner's host, whatever job the
    POS punch or the draft gave the shift: the usual crew read from the
    punches, the draft's own count and the generator's label all read a
    shift this way, so a requirement and its measurement never disagree on
    a label — EJ's week of 11/4/26 read "No host PM" on 11 shifts with three
    hosts on, the draft labelled them Host AM. An AM job's targets still
    keep to the morning (owner, 10/2/26): no "Host AM" is ever asked for at
    dinner."""
    if part not in ("morning", "night") or not str(role or "").strip():
        return role
    t = table if table is not None else fold_table(codes or (), families)
    return t.get((" ".join(str(role).split()).casefold(), part), role)


def name_key(name) -> str:
    """One spelling of a person's name for every comparison: case and
    spacing ignored — the key Constraints.salaried and models.
    salaried_name_key use ("erik  baylis" is "Erik Baylis")."""
    return " ".join(str(name or "").lower().split())
