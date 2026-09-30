"""owner_memory — what the owner (and the team) tell Cavnar AI, typed and
served to every model call (memory audit 9/29/26: owner_lanes, owner_reach,
owner_goals, conversations, sales_audit).

Three things live here:

  THE OWNER'S WORDS, typed. A fact is one sentence in someone's own terms
  with a kind (constraint | context | preference | goal | followup), the
  modules it is about (which generators read it), an optional subject tag,
  who said it (author and authority), who may read it (audience: team,
  principals, author), and a validity (valid_until for a time-bound fact,
  due_on for a follow-up). Stored in models' ask_memory, one lane per kind,
  with what the lanes and dates remove kept in ask_memory_archive.
  remember / forget / retract_for_keys / expire / facts_for are the one
  write and read path — Ask's tools, Account, the sales audit and the
  ratings all go through them, and every write drops Ask's cached snapshot.

  THE PROVIDERS memory_context reads: constraint_lines (the facts for the
  generator asking), goal_lines (active goals for the metrics in play) and
  conversation_lines (Ask's rolling chat summary, what its last answer
  read, and the questions a login keeps asking).

  THE TARGET RESOLVER: target_for(rid, metric) — the owner's active goal on
  a metric, which thresholds.target_for and every module that judges a
  figure read before their own setting.

Nothing here writes a model's words as the owner's: a model-written summary
is labelled as one and fenced like every other untrusted line.
"""
import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

KINDS = ("constraint", "context", "preference", "goal", "followup")
# A follow-up stays this many days past its due date, then leaves.
FOLLOWUP_GRACE_DAYS = 7

# Which modules' facts each memory_context surface reads. A fact with no
# modules is about the whole business and reaches every surface; None reads
# every fact.
SURFACE_MODULES = {
    "ask": None, "ask_conversation": None,
    "schedule": {"labor", "schedule", "ops"},
    "labor_read": {"labor", "schedule"},
    "food_read": {"food"},
    "food_diagnosis": {"food"},
    "review_diagnosis": {"reviews", "ops"},
    "reply_drafter": {"reviews"},
    "marketing": {"marketing", "guests"},
    "competitor_read": {"intel", "marketing"},
    "dsr_narrative": None, "brief": None, "digest": None, "weekly_plan": None,
}
# Follow-ups are for the surfaces an owner reads a to-do on.
FOLLOWUP_SURFACES = ("ask", "brief", "weekly_plan")

# The metrics whose goals each surface judges against (goal_lines). None:
# every goal. A surface not listed reads none.
SURFACE_METRICS = {
    "ask": None, "dsr_narrative": None, "brief": None, "digest": None, "weekly_plan": None,
    "labor_read": ("labor_pct", "overtime_hours", "weekday_sales"),
    "schedule": ("labor_pct", "overtime_hours", "weekday_sales", "sales"),
    "food_read": ("food_cost_pct", "weekly_waste"),
    "marketing": ("sales", "weekday_sales", "avg_rating"),
    # The Reviews read lists goals (memory_context.SURFACE_SECTIONS) and the
    # owner's rating or response-time goal is what it judges against; it
    # read none (INT wiring audit, 9/29/26).
    "review_read": ("avg_rating", "complaints", "response_hours"),
}
# The module whose view permission a goal's metric needs (memory_context
# viewer scoping reads it off the line).
_METRIC_MODULE = {"labor_pct": "labor", "overtime_hours": "labor", "sales": "labor", "weekday_sales": "labor",
                  "food_cost_pct": "food", "weekly_waste": "food", "avg_rating": "reviews",
                  "complaints": "reviews", "response_hours": "reviews", "comp_rate": "labor",
                  "void_rate": "labor"}

# Words that make a principal's fact private to the account holders by
# default, whatever audience the model asked for: personnel plans and money
# ("I'm letting Dana go in October" never reaches a manager's prompt).
_ROLE_WORDS_RE = r"(gm|general\s+manager|manager|chef|sous\s+chef|cook|line\s+cook|bartender|server|host|dishwasher|staff)"
_PRIVATE_RE = re.compile(
    r"\b(let(ting)?\s+\w+\s+go|(fire|fired|firing)\s+(him|her|them|my|our|the)\s+" + _ROLE_WORDS_RE + r"|"
    r"replac(e|ing)\s+(him|her|them|my|our|the)\s+" + _ROLE_WORDS_RE + r"|terminat\w*|"
    r"lay(ing)?\s*off|laid\s+off|salar(y|ies)|raise\s+for|pay\s+cut|payroll\s+for|sell(ing)?\s+the\s+"
    r"(restaurant|business|place)|lawsuit|lawyer|attorney|divorce|loan|debt|investor|partner(ship)?\s+"
    r"(split|buyout)|buy\s*out)\b", re.I)

# A measurable target said as a sentence ("labor under 26% by December"):
# the remember tool refuses it as a goal and points at set_goal, where it is
# measured and becomes the target the modules judge against.
_TARGET_RE = re.compile(r"\d+(\.\d+)?\s*(%|percent|points?\b)|\$\s?\d", re.I)
_METRIC_WORD_RE = re.compile(r"\b(labor|labour|food\s*cost|overtime|waste|rating|stars?|sales|revenue|"
                             r"prime\s*cost|reply\s*time|response\s*time)\b", re.I)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if db_path is None or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def _local_today(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _parse_day(value):
    """YYYY-MM-DD, M/D/YY or M/D/YYYY -> date, else None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", s)
    if m:
        y = int(m.group(3))
        y = 2000 + y if y < 100 else y
        try:
            return date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _mdy(value):
    from time_utils import mdy
    return mdy(value) if value else ""


# ── who said it ─────────────────────────────────────────────────────────────

_ROLE_WORDS = {"owner": "owner", "client": "owner", "manager": "manager", "member": "teammate",
               "employee": "staff", "support": "Cavnar AI support"}


def author_of(user) -> dict:
    """{"user_id", "label", "authority"} for the login writing a fact —
    "Erik, owner"; an admin (or anyone through view-as) is "Cavnar AI
    support", whose words never read as the owner's."""
    if not user:
        return {"user_id": None, "label": None, "authority": None}
    try:
        from permissions import answer_authority, normalize_role
        authority = answer_authority(user)
        role = normalize_role(user.get("role"))
    except Exception:
        authority, role = "delegate", str(user.get("role") or "")
    if authority == "admin":
        return {"user_id": user.get("id"), "label": "Cavnar AI support", "authority": "admin"}
    name = str(user.get("name") or user.get("username") or "").strip()
    if "@" in name:
        name = name.split("@", 1)[0]
    name = name[:1].upper() + name[1:] if name else ""
    word = _ROLE_WORDS.get(role, "owner" if authority == "principal" else "teammate")
    return {"user_id": user.get("id"), "label": f"{name}, {word}" if name else word.capitalize(),
            "authority": authority}


def _default_audience(kind, authority, text):
    if kind == "followup":
        return "author"
    if authority == "principal" and _PRIVATE_RE.search(text or ""):
        return "principals"
    return "team"


# A per-restaurant memory version, bumped by every memory write (remember,
# forget, retract, expire, restore, goals through goals._targets_changed):
# a front cache keyed on the data alone — labor.labor_note's — adds it to its
# key, so a constraint told at 10am is in the 10:01 read, not tomorrow's
# (memory re-audit 9/29/26, R3 labor_cache). Process-local, like the caches
# that read it.
_MEMORY_VERSION = {}


def memory_version(restaurant_id) -> int:
    try:
        return _MEMORY_VERSION.get(int(restaurant_id), 0)
    except (TypeError, ValueError):
        return 0


# The route caches of the reads whose prompt carries owner memory
# (client_api._insight_cache): dropped with the memory, like on an upload.
_MEMORY_READ_CACHES = ("labor-insight:", "mobile-labor-insight:", "inv-insight:", "mobile-inv-insight:",
                       "review-insight:")


def invalidate(restaurant_id):
    """A memory write reaches Ask's next answer, not one a minute later
    (ask_cavnar caches its snapshot for 60 s) — and the labor, food and
    review reads' next load, whose prompts carry the memory too (their
    front caches key on memory_version; their route caches are dropped)."""
    import sys
    try:
        rid = int(restaurant_id)
        _MEMORY_VERSION[rid] = _MEMORY_VERSION.get(rid, 0) + 1
    except (TypeError, ValueError):
        pass
    try:
        import ask_cavnar
        ask_cavnar.invalidate_context(restaurant_id)
    except Exception as e:
        log.debug("owner_memory: ask context not invalidated for %s: %s", restaurant_id, e)
    # Only where the web app is loaded (never imported for it: a scheduler
    # or a test has no route cache to drop).
    capi = sys.modules.get("client_api")
    if capi is not None:
        try:
            capi.invalidate_insight_cache(restaurant_id, prefixes=_MEMORY_READ_CACHES)
        except Exception as e:
            log.debug("owner_memory: read caches not dropped for %s: %s", restaurant_id, e)


# ── the write path ──────────────────────────────────────────────────────────

class MemoryRefused(ValueError):
    """A fact the store will not keep as asked; the message says what to do
    instead (set a goal, give a future date)."""


# A target said as a rule ("keep labor under 26%", "never let food cost pass
# 31%"): a figure, a metric word AND a direction. Without the direction a
# sentence with a number is background ("sales were $40k last week").
_DIRECTION_RE = re.compile(r"\b(under|below|over|above|at\s+most|at\s+least|no\s+more\s+than|less\s+than|"
                           r"more\s+than|keep|get|hit|reach|target|goal|aim|cap|max(imum)?|min(imum)?|pass|"
                           r"exceed|stay|by)\b", re.I)


def looks_like_target(text, kind="goal") -> bool:
    """"Labor under 26% by December" — a figure and a metric word: a goal
    set_goal can measure, not a sentence to keep. For any kind but "goal" a
    direction word is needed too, so a measured figure said as background is
    not refused (memory re-audit 9/29/26, R3 fact_conflicts: the check ran on
    "goal" only, so "keep labor under 26%" filed as a constraint was stored,
    and prompts said 26% while every module judged 28%)."""
    t = text or ""
    if not (_TARGET_RE.search(t) and _METRIC_WORD_RE.search(t)):
        return False
    return kind == "goal" or bool(_DIRECTION_RE.search(t))


# ── the same thing said twice, or a newer version of it ─────────────────────
#
# The only dedupe was exact text, so paraphrases took two slots and a
# contradiction ("we're closed Mondays" / "we're open Mondays now") reached
# the same prompt twice. A new fact is compared with what the writer can see
# of the same kind (or the same subject); the close ones are handed back
# ("this replaces …?"), and `replaces` archives the older one as 'replaced'.
_STOP_WORDS = frozenset(
    "a an the and or but to of in on at for with from by is are was were be been being it its this that "
    "these those we our us you your they their them he she his her him i me my shes hes theyre were weve "
    "im ive youre ill wed as so if then than there here about into up out just also very really please "
    "has have had will would should could can may might".split())
_POLARITY_WORDS = frozenset(
    "open opens opened opening closed close closes closing never always not no dont doesnt didnt isnt arent "
    "wasnt werent cant cannot wont do does now anymore again stop stops stopped start starts started keep "
    "only yes".split())


def _tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'", "").replace("’", "")):
        if w in _STOP_WORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _is_similar(a_text, b_text, a_subject=None, b_subject=None) -> bool:
    if a_subject and b_subject and str(a_subject).strip().lower() == str(b_subject).strip().lower():
        return True
    a, b = _tokens(a_text), _tokens(b_text)
    if not a or not b:
        return False
    if len(a & b) / len(a | b) >= 0.5:
        return True
    ca, cb = a - _POLARITY_WORDS, b - _POLARITY_WORDS
    if ca and ca == cb:
        return True                    # the same thing with its polarity flipped
    small = min(len(ca), len(cb))
    return small >= 2 and len(ca & cb) * 3 >= small * 2


def similar_facts(restaurant_id, text, kind=None, subject=None, user=None, exclude_ids=(), db_path=None,
                  limit=3) -> list:
    """The live facts `user` may read that look like `text` — the same
    kind with most of the same words, the same subject, or the same words
    with the polarity flipped. [{"id", "fact", "kind"}], closest first."""
    out = []
    skip = {int(i) for i in exclude_ids or () if i is not None}
    for r in facts_for(restaurant_id, viewer=user, db_path=db_path, include_expired=True):
        if r["id"] in skip or r["fact"] == text:
            continue
        same_subject = subject and r.get("subject") and str(r["subject"]).lower() == str(subject).lower()
        if kind and (r.get("kind") or "context") != kind and not same_subject:
            continue
        if _is_similar(text, r["fact"], subject, r.get("subject")):
            out.append({"id": r["id"], "fact": r["fact"], "kind": r.get("kind") or "context"})
    return out[:limit]


_AUDIENCE_RANK = {"author": 0, "principals": 1, "team": 2}


def _narrower(a, b):
    """The more restrictive of two audiences (author < principals < team)."""
    a = a if a in _AUDIENCE_RANK else "team"
    b = b if b in _AUDIENCE_RANK else "team"
    return a if _AUDIENCE_RANK[a] <= _AUDIENCE_RANK[b] else b


def remember(restaurant_id, fact, kind="context", modules=None, subject=None, valid_until=None, due_on=None,
             audience=None, user=None, source=None, origin="ask", author_label=None, db_path=None,
             today=None, replaces=None, scope=None) -> dict:
    """Keep one fact. Returns {"fact", "kind", "evicted", "evicted_facts",
    "audience", "valid_until", "due_on", "id", "similar", "replaced",
    "confirmed"}; raises MemoryRefused (a ValueError) for an empty fact, a
    past date, or a measurable target (any kind — memory re-audit R3).
    `scope` "org": every location of the organisation reads it — a group
    owner only (may_set_org; memory re-audit R6, PEOPLE-13).

    `user` is the login saying it — its id, label and authority are stored
    on the fact, so a manager's remark is never shown as the owner's; None
    is a seeded fact, labelled by `author_label` ("Sales audit 9/8/26").

    The same words another login already said (memory re-audit 9/29/26, R3
    fact_text_key): when this login can read that fact, saying it again is a
    CONFIRMATION — stamped on it (confirmed_by), its author, audience and
    dates untouched; when it cannot (the owner's own), this login's words
    are its own row, never wider than the one already there.

    `replaces` (a fact's id or exact text this login may drop) archives that
    fact as 'replaced' once this one is kept; `similar` hands back the close
    ones so the caller can ask "does this replace …?"."""
    import models
    import memory_context
    text = " ".join(str(fact or "").split())[:models.ASK_MEMORY_MAX_LENGTH]
    if not text:
        raise MemoryRefused("a fact needs some text")
    kind = kind if kind in KINDS else "context"
    if origin not in ("audit", "ratings") and looks_like_target(text, kind):
        raise MemoryRefused("That's a measurable target — set it as a goal (set_goal, or Goals) so it is "
                            "measured and every module judges against it.")
    today = today or _local_today(restaurant_id)
    until = _parse_day(valid_until)
    due = _parse_day(due_on)
    if valid_until and until is None:
        raise MemoryRefused("valid_until must be a date (YYYY-MM-DD)")
    if due_on and due is None:
        raise MemoryRefused("due_on must be a date (YYYY-MM-DD)")
    if until is not None and until < today:
        raise MemoryRefused("that date has already passed")
    if kind == "followup" and due is None:
        due = today + timedelta(days=7)
    who = author_of(user)
    if audience not in ("team", "principals", "author"):
        audience = _default_audience(kind, who["authority"], text)
    elif who["authority"] == "principal" and audience == "team" and _PRIVATE_RE.search(text):
        # The backstop: personnel and money said by an account holder stay
        # theirs, whatever the model asked for.
        audience = "principals"
    if isinstance(modules, str):
        modules = [m.strip() for m in modules.split(",")]
    if scope == "org" and not may_set_org(restaurant_id, user, db_path=db_path):
        raise MemoryRefused("Only the owner of every location can keep a fact for all of them.")
    viewer = memory_context.viewer_user(user)
    same = [r for r in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH)
            if r["fact"] == text and r.get("user_id") != who["user_id"]]
    if user is not None:
        seen = [r for r in same if memory_context.visible(
            {"audience": r.get("audience") or "team", "author_id": r.get("user_id")}, viewer)]
        if seen:
            row = seen[0]
            models.confirm_ask_fact(restaurant_id, row["id"], confirmed_by=author_label or who["label"],
                                    db_path=db_path or models.DB_PATH)
            if scope == "org":
                models.set_ask_fact_scope(restaurant_id, row["id"], "org", db_path=db_path or models.DB_PATH)
            invalidate(restaurant_id)
            return {"fact": row["fact"], "kind": row.get("kind") or "context", "evicted": 0, "evicted_facts": [],
                    "audience": row.get("audience") or "team", "valid_until": row.get("valid_until"),
                    "due_on": row.get("due_on"), "id": row["id"], "similar": [], "replaced": None,
                    "confirmed": True}
    for r in same:
        audience = _narrower(audience, r.get("audience") or "team")
    saved = models.remember_ask_fact(
        restaurant_id, text, kind=kind, source=source, user_id=who["user_id"], db_path=db_path or models.DB_PATH,
        modules=list(modules or ()), subject=subject, audience=audience,
        author_label=author_label or who["label"], authority=who["authority"] or ("system" if user is None else None),
        valid_until=until.isoformat() if until else None, due_on=due.isoformat() if due else None, origin=origin,
        scope="org" if scope == "org" else None)
    replaced = None
    if replaces not in (None, "", 0):
        replaced = _replace(restaurant_id, replaces, user, keep_id=saved.get("id"), db_path=db_path)
    similar = []
    if user is not None and replaced is None:
        similar = similar_facts(restaurant_id, text, kind=kind, subject=subject, user=user,
                                exclude_ids=[saved.get("id")], db_path=db_path)
    evicted_seen = [{"fact": e["fact"]} for e in saved.get("evicted_facts") or []
                    if memory_context.visible({"audience": e.get("audience") or "team",
                                               "author_id": e.get("user_id")}, viewer)]
    expire(restaurant_id, today=today, db_path=db_path)
    invalidate(restaurant_id)
    return {"fact": saved["fact"], "kind": saved["kind"], "evicted": saved.get("evicted", 0),
            "evicted_facts": evicted_seen, "audience": saved.get("audience") or audience,
            "valid_until": until.isoformat() if until else None, "due_on": due.isoformat() if due else None,
            "id": saved.get("id"), "similar": similar, "replaced": replaced, "confirmed": False}


def _replace(restaurant_id, target, user, keep_id=None, db_path=None):
    """Archive the fact `target` (an id or its exact text) as 'replaced' —
    one `user` may read and drop. Returns its text, or None."""
    import models
    rows = facts_for(restaurant_id, viewer=user, db_path=db_path, include_expired=True)
    try:
        tid = int(target)
        row = next((r for r in rows if r["id"] == tid), None)
    except (TypeError, ValueError):
        t = " ".join(str(target or "").split())
        row = next((r for r in rows if r["fact"] == t), None)
    if row is None or row["id"] == keep_id or not _may_edit(row, user):
        return None
    models.archive_ask_facts(restaurant_id, [row["id"]], "replaced", archived_by=(user or {}).get("id"),
                             db_path=db_path or models.DB_PATH)
    return row["fact"]


def _may_edit(fact_row, user) -> bool:
    """Who may forget a fact: an account holder (or an internal caller) any
    fact; anyone else only what they wrote themselves."""
    if user is None:
        return True
    try:
        from permissions import answer_authority
        if answer_authority(user) in ("principal", "admin"):
            return True
    except Exception:
        pass
    return fact_row.get("user_id") is not None and fact_row.get("user_id") == user.get("id")


def forget(restaurant_id, fact, user=None, db_path=None) -> dict:
    """Drop one fact. Exact text first; otherwise a loose match, but only
    when exactly ONE readable fact matches (it used to drop the first of
    several). {"forgotten": text} | {"error", "candidates"?, "remembered"?}."""
    import models
    text = " ".join(str(fact or "").split())
    if not text:
        return {"error": "name the note to drop"}
    rows = facts_for(restaurant_id, viewer=user, db_path=db_path, include_expired=True)
    exact = [r for r in rows if r["fact"] == text]
    if not exact:
        low = text.lower()
        exact = [r for r in rows if low in r["fact"].lower() or r["fact"].lower() in low]
        if len(exact) > 1:
            return {"error": "more than one note matches — say which", "candidates": [r["fact"] for r in exact][:6]}
    if not exact:
        return {"error": "no note like that", "remembered": [r["fact"] for r in rows][:30]}
    # This login's own row first: the same words from someone else are theirs.
    exact.sort(key=lambda r: r.get("user_id") != (user or {}).get("id"))
    row = exact[0]
    if row.get("from_location") is not None:
        return {"error": "that note is kept for every location at another of your locations; forget it there"}
    if not _may_edit(row, user):
        return {"error": "that note was added by someone else; only its author or the owner can drop it"}
    if row.get("origin") == "ratings":
        # A preference read off their ratings, forgotten: kept in the archive
        # as 'forgotten' so the next rating does not simply put it back
        # (derive_rating_preferences reads it).
        models.archive_ask_facts(restaurant_id, [row["id"]], "forgotten",
                                 archived_by=(user or {}).get("id"), db_path=db_path or models.DB_PATH)
    else:
        # By id: the same words from another login are their own fact.
        models.delete_ask_facts(restaurant_id, [row["id"]], db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return {"forgotten": row["fact"]}


# ── preferences read off the owner's own ratings (ask_feedback) ─────────────
#
# Five "not helpful — too long, just give me the number" notes used to change
# nothing. The pattern becomes a preference fact — visible in Account,
# labelled as coming from their ratings, forgettable — and the depth Ask
# chooses for that login (memory audit 9/29/26, ask_feedback).

RATING_PREF_MIN = 3
RATING_PREF_SUBJECT = "ask:answer_length"
_WANTS_SHORT_RE = re.compile(r"\b(too long|shorter|too much|too wordy|wordy|just (give me )?the (number|answer|figure)|"
                             r"get to the point|tl;?dr|less text|keep it short|be brief)\b", re.I)
_WANTS_MORE_RE = re.compile(r"\b(more detail|more details|explain|too short|not enough|go deeper|elaborate|"
                            r"what does that mean|why\?|say why|the reasoning)\b", re.I)


def _rating_pref_rows(restaurant_id, user_id, db_path=None):
    import models
    return [f for f in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH)
            if str(f.get("subject") or "").startswith(RATING_PREF_SUBJECT) and f.get("user_id") == user_id
            and f.get("origin") == "ratings"]


def _org_others(restaurant_id, db_path=None) -> list:
    """The organisation's other locations (preferences.org_location_ids)."""
    try:
        import preferences
        return preferences.org_location_ids(restaurant_id, db_path=db_path)
    except Exception:
        return []


def rating_preference(restaurant_id, user_id, db_path=None):
    """"short" | "full" | None — the answer length this login's ratings ask
    for (the live derived fact; a forgotten one is gone). A person, not a
    location (memory re-audit 9/29/26, PEOPLE-13): with none derived here,
    the one their ratings set at another of the organisation's locations
    applies — a three-location owner said "shorter" once, not three times."""
    if user_id is None:
        return None
    for rid in [restaurant_id] + _org_others(restaurant_id, db_path=db_path):
        for f in _rating_pref_rows(rid, user_id, db_path=db_path):
            return str(f["subject"]).rsplit(":", 1)[-1] if str(f["subject"]).count(":") >= 2 else None
    return None


def _rating_signals(rows):
    """What a set of ratings says about length: the notes asking for
    shorter, the long answers rated not helpful, the notes asking for more
    on a short answer, and the helpful ratings of long and of short answers."""
    bad = [r for r in rows if not r.get("helpful")]
    long_rated = [r for r in rows if r.get("depth") == "executive"]
    long_bad = [r for r in long_rated if not r.get("helpful")]
    return {
        "wants_short": [r for r in bad if _WANTS_SHORT_RE.search(str(r.get("note") or ""))],
        "long_rated": long_rated, "long_bad": long_bad,
        "wants_more": [r for r in bad if r.get("depth") in ("brief", "standard")
                       and _WANTS_MORE_RE.search(str(r.get("note") or ""))],
        "long_good": [r for r in long_rated if r.get("helpful")],
        "short_good": [r for r in rows if r.get("helpful") and r.get("depth") in ("brief", "standard")],
    }


def _short_support(sig) -> int:
    """How many ratings ask for shorter: the notes, or the long answers
    rated not helpful when they are most of the long ones rated."""
    n = len(sig["wants_short"])
    if len(sig["long_bad"]) >= RATING_PREF_MIN and 2 * len(sig["long_bad"]) > len(sig["long_rated"]):
        n = max(n, len(sig["long_bad"]))
    return n


def _pref_text(pref, sig):
    if pref == "short":
        basis = (f"{len(sig['wants_short'])} answers rated not helpful as too long"
                 if len(sig["wants_short"]) >= RATING_PREF_MIN
                 else f"{len(sig['long_bad'])} of {len(sig['long_rated'])} long answers rated not helpful")
        return f"Prefers short, direct answers — lead with the number ({basis})"
    return (f"Prefers fuller answers with the reasoning ({len(sig['wants_more'])} short answers rated not helpful "
            f"as too thin)")


def derive_rating_preferences(restaurant_id, user, db_path=None):
    """Read this login's ratings for a length preference and keep it as a
    fact (kind preference, origin "ratings", visible to them alone). Returns
    {"preference", "fact"} or None.

    Memory re-audit 9/29/26 (R3 rating_pref): a preference used to be
    re-read from the last 180 days on every rating — so once the "too long"
    notes aged out, the next rating (even a helpful one on a short answer:
    the preference WORKING) deleted it, unarchived; and "short" was tested
    first, so newer "explain more" notes could never reverse it. Now:
      - with no preference, the side with RATING_PREF_MIN ratings and more
        of them than the other side is kept;
      - a kept preference is judged only on the ratings SINCE it was derived
        (its created_at): the opposite side reaching RATING_PREF_MIN and
        outnumbering its own support REPLACES it (archived 'replaced'); the
        answers it avoids rated helpful that often RETIRES it (archived
        'contradicted', shown in Account); its own side again stamps it
        confirmed (last supported) — and nothing else ever removes it;
      - one they forgot is not re-derived until RATING_PREF_MIN ratings
        since the forget say it again (the forget marker is looked up by
        this login and subject, not within the newest 200 archive rows)."""
    import models
    uid = (user or {}).get("id") if isinstance(user, dict) else user
    if uid is None:
        return None
    dbp = db_path or models.DB_PATH
    # Their ratings at every location of the organisation: the person's own
    # habit, read once, not relearned per location (R6, PEOPLE-13).
    rows = models.ask_feedback_rows(restaurant_id, user_id=uid, db_path=dbp)
    for other in _org_others(restaurant_id, db_path=db_path):
        rows += models.ask_feedback_rows(other, user_id=uid, db_path=dbp)
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    forgot = models.get_ask_memory_archive(restaurant_id, limit=1, db_path=dbp, user_id=uid, reason="forgotten",
                                           subject_prefix=RATING_PREF_SUBJECT)
    if forgot:
        since = str(forgot[0].get("archived_at") or "")
        rows = [r for r in rows if str(r.get("updated_at") or "") > since]
    old = _rating_pref_rows(restaurant_id, uid, db_path=db_path)
    current = old[0] if old else None
    if current is not None:
        cur_pref = str(current.get("subject") or "").rsplit(":", 1)[-1]
        derived_at = str(current.get("created_at") or "")
        after = [r for r in rows if str(r.get("updated_at") or "") >= derived_at]
        sig = _rating_signals(after)
        support = _short_support(sig) if cur_pref == "short" else len(sig["wants_more"])
        opposite = len(sig["wants_more"]) if cur_pref == "short" else _short_support(sig)
        against = len(sig["long_good"]) if cur_pref == "short" else len(sig["short_good"])
        if opposite >= RATING_PREF_MIN and opposite > support:
            new_pref = "full" if cur_pref == "short" else "short"
            models.archive_ask_facts(restaurant_id, [f["id"] for f in old], "replaced", db_path=dbp)
            text = _pref_text(new_pref, sig)
            _store_rating_pref(restaurant_id, uid, new_pref, text, dbp)
            invalidate(restaurant_id)
            return {"preference": new_pref, "fact": text}
        if against >= RATING_PREF_MIN and against > support:
            models.archive_ask_facts(restaurant_id, [f["id"] for f in old], "contradicted", db_path=dbp)
            invalidate(restaurant_id)
            return None
        consistent = len(sig["short_good"]) if cur_pref == "short" else len(sig["long_good"])
        if support or consistent:
            # Still supported (its own notes again, or the answers it shapes
            # rated helpful): stamp when (last supported), and let the
            # wording carry the newest count — its date and author unchanged.
            full = _rating_signals(rows)
            if (_short_support(full) if cur_pref == "short" else len(full["wants_more"])) >= RATING_PREF_MIN:
                text = _pref_text(cur_pref, full)
            else:
                text = current["fact"]
            models.confirm_ask_fact(restaurant_id, current["id"], confirmed_by="Their ratings",
                                    fact=text if text != current["fact"] else None, db_path=dbp)
            invalidate(restaurant_id)
            return {"preference": cur_pref, "fact": text}
        return {"preference": cur_pref, "fact": current["fact"]}
    sig = _rating_signals(rows)
    short_n, full_n = _short_support(sig), len(sig["wants_more"])
    pref = None
    if short_n >= RATING_PREF_MIN and short_n > full_n:
        pref = "short"
    elif full_n >= RATING_PREF_MIN and full_n > short_n:
        pref = "full"
    if pref is None:
        return None
    text = _pref_text(pref, sig)
    _store_rating_pref(restaurant_id, uid, pref, text, dbp)
    invalidate(restaurant_id)
    return {"preference": pref, "fact": text}


def _store_rating_pref(restaurant_id, uid, pref, text, db_path):
    import models
    models.remember_ask_fact(restaurant_id, text, kind="preference", source="Your ratings", user_id=uid,
                             db_path=db_path, modules=None, subject=f"{RATING_PREF_SUBJECT}:{pref}",
                             audience="author", author_label="From their own ratings", authority="system",
                             origin="ratings")


def retract_for_keys(restaurant_id, keys, titles=(), archived_by=None, db_path=None) -> int:
    """"Use again" on a card (home_brief.undismiss) or a restored kind
    (decisions.restore_kind): the owner reversed an answer, so a remembered
    "Not doing X" about it must stop steering answers away from X. Archives
    (reason 'retracted') every fact whose subject is one of `keys`, and the
    legacy Home facts ("Not doing “<title or key>”: …") for them. Returns how
    many facts moved."""
    import models
    keys = [str(k).strip() for k in keys or () if str(k or "").strip()]
    if not keys:
        return 0
    names = set(keys) | {str(t).strip() for t in titles or () if str(t or "").strip()}
    try:
        conn = get_conn(db_path)
        try:
            for k in keys:
                for r in conn.execute("SELECT title FROM rec_instances WHERE restaurant_id=? AND key=? "
                                      "AND title IS NOT NULL", (restaurant_id, k)).fetchall():
                    names.add(str(r["title"]).strip())
        finally:
            conn.close()
    except Exception:
        pass
    ids = []
    for f in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH):
        subj = str(f.get("subject") or "").strip()
        text = f.get("fact") or ""
        if subj and subj in keys:
            ids.append(f["id"])
            continue
        if text.startswith("Not doing “") and "”: " in text:
            title = text[len("Not doing “"):].split("”: ", 1)[0]
            if any(title == n[:80] for n in names):
                ids.append(f["id"])
    if not ids:
        return 0
    n = models.archive_ask_facts(restaurant_id, ids, "retracted", archived_by=archived_by,
                                 db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return n


def departed_author_ids(restaurant_id, db_path=None) -> set:
    """The logins behind this restaurant's facts who no longer have access
    to it: switched off (users.is_active=0 — a revoke, an admin's
    deactivation), or whose membership of this restaurant was ended
    (memberships.is_active=0, and it is not their own login's restaurant).
    Deliberately NOT inferred from a missing membership: a group owner
    reaches a sibling location through the location group, with no
    membership row (auth._still_in_group). Support's login is never
    "departed", and an id with no users row is not judged."""
    try:
        conn = get_conn(db_path)
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT user_id FROM ask_memory WHERE restaurant_id=? AND user_id IS NOT NULL",
                (restaurant_id,)).fetchall()]
            if not ids:
                return set()
            marks = ",".join("?" for _ in ids)
            try:
                gone = {r[0] for r in conn.execute(
                    f"SELECT u.id FROM users u WHERE u.id IN ({marks}) AND COALESCE(u.is_admin, 0)=0 AND "
                    "(COALESCE(u.is_active, 1)=0 OR (COALESCE(u.restaurant_id, -1)!=? AND EXISTS "
                    "(SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.restaurant_id=? AND m.is_active=0)))",
                    (*ids, restaurant_id, restaurant_id)).fetchall()}
            except Exception:
                gone = {r[0] for r in conn.execute(
                    f"SELECT u.id FROM users u WHERE u.id IN ({marks}) AND COALESCE(u.is_admin, 0)=0 AND "
                    "COALESCE(u.is_active, 1)=0", ids).fetchall()}
        finally:
            conn.close()
    except Exception as e:
        log.debug("owner_memory: departed logins unreadable for %s: %s", restaurant_id, e)
        return set()
    return gone


def retire_departed(restaurant_id, db_path=None) -> int:
    """A login that left takes its private notes with it (memory re-audit
    9/29/26, R3 revoked_login): its "author" facts — which no one else could
    read, and which still steered every internal prompt — move to the
    archive as 'author_left', where the account holders see them and may put
    one back as their own. Its shared facts stay (they were said to the
    team) and Account marks them as from someone who left, for the owner to
    keep or forget. Called by auth.revoke_team_member, and by every expiry
    pass for a login switched off any other way. Returns how many moved."""
    import models
    gone = departed_author_ids(restaurant_id, db_path=db_path)
    if not gone:
        return 0
    ids = [f["id"] for f in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH)
           if f.get("user_id") in gone and (f.get("audience") or "team") == "author"]
    if not ids:
        return 0
    n = models.archive_ask_facts(restaurant_id, ids, "author_left", db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return n


def expire(restaurant_id, today=None, db_path=None) -> int:
    """Archive what has run out: a time-bound fact past its valid_until, a
    follow-up FOLLOWUP_GRACE_DAYS past its due date — and a departed login's
    private notes (retire_departed). Returns how many."""
    import models
    today = today or _local_today(restaurant_id)
    left = 0
    try:
        left = retire_departed(restaurant_id, db_path=db_path)
    except Exception as e:
        log.debug("owner_memory: departed notes not retired for %s: %s", restaurant_id, e)
    ids = []
    for f in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH):
        until = _parse_day(f.get("valid_until"))
        due = _parse_day(f.get("due_on"))
        if until is not None and until < today:
            ids.append(f["id"])
        elif (f.get("kind") == "followup" and due is not None
              and due + timedelta(days=FOLLOWUP_GRACE_DAYS) < today):
            ids.append(f["id"])
    if not ids:
        return left
    n = models.archive_ask_facts(restaurant_id, ids, "expired", db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return n + left


def mark_used(restaurant_id, fact_ids, db_path=None) -> int:
    """memory_context: these facts were just carried into a model prompt.
    Lane eviction reads it — least used goes first, not oldest written
    (memory re-audit 9/29/26, R3 lane_eviction). Never raises."""
    try:
        import models
        n = models.mark_ask_facts_used(restaurant_id, fact_ids, db_path=db_path or models.DB_PATH)
        # An organisation-wide fact kept at another location (R6, PEOPLE-13)
        # was used too: stamped where it lives, so that location's lane
        # budget does not evict it as unused while every sibling reads it.
        # Fact ids are unique across locations, so only those rows match.
        for other in _org_others(restaurant_id, db_path=db_path) if fact_ids else ():
            n += models.mark_ask_facts_used(other, fact_ids, db_path=db_path or models.DB_PATH)
        return n
    except Exception as e:
        log.debug("owner_memory: use not stamped for %s: %s", restaurant_id, e)
        return 0


# ── the read path ───────────────────────────────────────────────────────────

def _modules_of(row):
    return {m for m in str(row.get("modules") or "").split(",") if m}


def facts_for(restaurant_id, viewer=None, surface=None, kinds=None, today=None, db_path=None,
              include_expired=False) -> list:
    """The live facts `viewer` may read (memory_context's own rule, applied
    here too so Account and the tools agree with the prompt), for `surface`
    (SURFACE_MODULES) and `kinds`. None viewer: every fact (internal)."""
    import models
    import memory_context
    today = today or _local_today(restaurant_id)
    if not include_expired:
        try:
            expire(restaurant_id, today=today, db_path=db_path)
        except Exception as e:
            log.debug("owner_memory: expiry skipped for %s: %s", restaurant_id, e)
    rows = models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH, kinds=kinds)
    rows += _org_facts(restaurant_id, rows, kinds=kinds, today=today, db_path=db_path)
    user = memory_context.viewer_user(viewer)
    authority = memory_context._authority(user)
    want = SURFACE_MODULES.get(surface, None) if surface else None
    out = []
    for r in rows:
        line = {"audience": r.get("audience") or "team", "author_id": r.get("user_id")}
        if not memory_context.visible(line, user, authority):
            continue
        mods = _modules_of(r)
        if want is not None and mods and not (mods & want):
            continue
        out.append(r)
    return out


def _org_facts(restaurant_id, have, kinds=None, today=None, db_path=None) -> list:
    """The organisation-wide facts the group's other locations keep (scope
    'org', memory re-audit 9/29/26, PEOPLE-13): "we close every location on
    Thanksgiving", told at one location, reaches the others' schedules and
    nightly reports. Each carries `from_location` (its id); a date that has
    passed is left out (its own location's expiry archives it); a text this
    location already keeps is not repeated. They pass the same visibility
    rule as this location's own facts (facts_for)."""
    import models
    others = _org_others(restaurant_id, db_path=db_path)
    if not others:
        return []
    today = today or _local_today(restaurant_id)
    mine = {str(r.get("fact") or "").strip().lower() for r in have or []}
    out = []
    for r in models.org_ask_memory(others, db_path=db_path or models.DB_PATH, kinds=kinds):
        until = _parse_day(r.get("valid_until"))
        if until is not None and until < today:
            continue
        key = str(r.get("fact") or "").strip().lower()
        if key in mine:
            continue
        mine.add(key)
        out.append(dict(r, from_location=r.get("restaurant_id")))
    return out


def may_set_org(restaurant_id, user, db_path=None) -> bool:
    """Whether `user` may keep a fact for every location: a group owner —
    an account holder who may switch between the organisation's locations
    (preferences.may_apply_to_all, the rule every organisation-wide setting
    uses)."""
    if not isinstance(user, dict) or not user:
        return False
    try:
        import models
        import preferences
        r = models.get_restaurant(restaurant_id, db_path) if db_path else models.get_restaurant(restaurant_id)
        return preferences.may_apply_to_all(user, r)
    except Exception:
        return False


def set_scope(restaurant_id, fact_id, scope, user, db_path=None) -> dict:
    """"For every location" / "this location only" on one of this
    location's facts. Only a group owner (may_set_org). Raises
    MemoryRefused."""
    import models
    if scope not in ("org", "location"):
        raise MemoryRefused("scope is org or location")
    if not may_set_org(restaurant_id, user, db_path=db_path):
        raise MemoryRefused("Only the owner of every location can keep a fact for all of them.")
    if not models.set_ask_fact_scope(restaurant_id, int(fact_id), scope, db_path=db_path or models.DB_PATH):
        raise MemoryRefused("That fact isn't kept at this location.")
    invalidate(restaurant_id)
    for other in _org_others(restaurant_id, db_path=db_path):
        invalidate(other)
    return {"id": int(fact_id), "scope": scope}


def _who(row):
    label = str(row.get("author_label") or "").strip()
    if label:
        return label
    src = str(row.get("source") or "").strip()
    return src or None


_KIND_WEIGHT = {"constraint": 3.0, "context": 2.0, "preference": 1.5, "followup": 2.5, "goal": 1.0}


def _line(row, weight_bonus=0.0):
    kind = row.get("kind") or "context"
    mods = sorted(_modules_of(row))
    text = row["fact"]
    if kind == "followup" and row.get("due_on"):
        text = f"Follow up by {_mdy(row['due_on'])}: {text}"
    elif kind == "constraint":
        text = f"Constraint: {text}"
    elif kind == "preference":
        text = f"Preference: {text}"
    who = _who(row)
    if row.get("from_location") is not None:
        who = f"{who or 'the owner'}, for every location"
    return {"text": text, "date": str(row.get("created_at") or "")[:10] or None, "source": "owner",
            "subject": row.get("subject") or (mods[0] if len(mods) == 1 else None),
            "weight": _KIND_WEIGHT.get(kind, 1.0) + weight_bonus, "trusted": False,
            "who": who, "until": row.get("valid_until"), "audience": row.get("audience") or "team",
            "author_id": row.get("user_id"), "module": mods[0] if len(mods) == 1 else None,
            "kind": kind, "fact_id": row.get("id")}


def constraint_lines(req):
    """memory_context provider: the owner's and the team's facts for
    req.surface — constraints, context, preferences, and on the owner's
    own reading surfaces the follow-ups coming due — fenced, dated, with
    who said each. A fact about this surface's modules outranks a general
    one. Goal-kind facts (aims no metric reads) go with the goals."""
    surface = getattr(req, "surface", None)
    kinds = ["constraint", "context", "preference"]
    if surface in FOLLOWUP_SURFACES or surface is None:
        kinds.append("followup")
    rows = facts_for(req.restaurant_id, viewer=None, surface=surface, kinds=kinds, db_path=req.db_path)
    want = SURFACE_MODULES.get(surface) if surface else None
    today = _local_today(req.restaurant_id)
    out = []
    for r in rows:
        if r.get("kind") == "followup":
            due = _parse_day(r.get("due_on"))
            # A follow-up is said from a week before it is due.
            if due is not None and due - timedelta(days=7) > today:
                continue
        bonus = 0.5 if (want and (_modules_of(r) & want)) else 0.0
        out.append(_line(r, bonus))
    return out


def goal_lines(req):
    """memory_context provider: the owner's active goals for the metrics in
    play on req.surface, each with where it stands (goals.progress — the
    measured reading, trusted) and who set it; a goal a teammate proposed
    that is waiting for the owner; and the owner's goal-kind facts (aims no
    metric reads), fenced."""
    surface = getattr(req, "surface", None)
    if surface not in SURFACE_METRICS and surface is not None:
        want_metrics = ()
    else:
        want_metrics = SURFACE_METRICS.get(surface) if surface else None
    out = []
    try:
        import goals
        rows = goals.progress(req.restaurant_id, db_path=req.db_path or _default_db())
        proposed = goals.proposed(req.restaurant_id, db_path=req.db_path or _default_db())
    except Exception as e:
        log.debug("owner_memory: goals unreadable for %s: %s", req.restaurant_id, e)
        rows, proposed = [], []
    labels = _user_labels([g.get("created_by") for g in rows + proposed], db_path=req.db_path)

    def _wanted(metric):
        base = str(metric or "").split(":", 1)[0]
        return want_metrics is None or base in (want_metrics or ())
    for g in rows:
        if not _wanted(g.get("metric")):
            continue
        base = str(g.get("metric") or "").split(":", 1)[0]
        state = g.get("state")
        who = labels.get(g.get("created_by"))
        out.append({"text": "Goal — " + goals.summarise(g), "date": str(g.get("created_at") or "")[:10] or None,
                    "source": "system", "trusted": True, "subject": base,
                    "weight": 3.0 if state in ("moving_wrong_way", "missed", "flat") else 2.0,
                    "who": f"set by {who}" if who else None, "module": _METRIC_MODULE.get(base)})
    for g in proposed:
        if not _wanted(g.get("metric")):
            continue
        base = str(g.get("metric") or "").split(":", 1)[0]
        who = ("the sales audit" if g.get("source") == "audit"
               else labels.get(g.get("created_by")) or "a teammate")
        out.append({"text": f"Proposed goal, waiting for the owner to confirm — {goals.describe_target(g)}",
                    "date": str(g.get("created_at") or "")[:10] or None, "source": "system", "trusted": True,
                    "subject": base, "weight": 0.5, "who": f"proposed by {who}", "module": _METRIC_MODULE.get(base),
                    "audience": "principals", "author_id": g.get("created_by")})
    if surface in (None, "ask", "brief", "weekly_plan", "dsr_narrative", "digest"):
        for r in facts_for(req.restaurant_id, viewer=None, surface=surface, kinds=["goal"], db_path=req.db_path):
            line = _line(r)
            line["text"] = f"Aim: {r['fact']}"
            out.append(line)
    return out


def _default_db():
    import models
    return models.DB_PATH


def _user_labels(user_ids, db_path=None) -> dict:
    """{user_id: "Erik, owner"} for the logins behind goals."""
    ids = sorted({int(u) for u in user_ids or () if u is not None})
    if not ids:
        return {}
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(f"SELECT id, username, role, is_admin FROM users WHERE id IN "
                                f"({','.join('?' for _ in ids)})", ids).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    return {r["id"]: author_of(dict(r))["label"] for r in rows}


def conversation_lines(req):
    """memory_context provider (Ask only): for a "conversation:<id>" in
    req.subjects, the chat's rolling summary and what its last answer read;
    and the questions this login keeps asking. See ask_conversations."""
    try:
        import ask_conversations
        return ask_conversations.memory_lines(req)
    except ImportError:
        return []


# ── the target a module judges against ─────────────────────────────────────

# The spellings callers use for a goals metric (metrics._REGISTRY keys).
_METRIC_ALIASES = {"labor": "labor_pct", "labor_pct": "labor_pct", "food": "food_cost_pct",
                   "food_cost": "food_cost_pct", "food_cost_pct": "food_cost_pct", "nightly_sales": "sales",
                   "daily_sales": "sales", "sales": "sales", "overtime": "overtime_hours",
                   "overtime_hours": "overtime_hours", "waste": "weekly_waste", "weekly_waste": "weekly_waste",
                   "rating": "avg_rating", "avg_rating": "avg_rating", "response_hours": "response_hours",
                   "prime_cost_pct": "prime_cost_pct"}
def _request_memo():
    """A per-Flask-request memo for target_for (a Home build reads the same
    target many times), like models' per-request get_restaurant: None off a
    request (the scheduler, tests, scripts), so a long job sees a goal the
    moment it is set and nothing outlives the request that read it."""
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return None
        memo = getattr(g, "_owner_targets", None)
        if memo is None:
            memo = {}
            g._owner_targets = memo
        return memo
    except Exception:
        return None


def invalidate_targets(restaurant_id=None):
    """Called by every goals write, so a goal set is the target at once —
    even later in the same request."""
    memo = _request_memo()
    if memo is None:
        return
    if restaurant_id is None:
        memo.clear()
    else:
        for k in [k for k in memo if k[0] == int(restaurant_id)]:
            memo.pop(k, None)


def _goal_label(metric, value, deadline):
    try:
        import metrics
        unit = metrics.describe(metric)["unit"]
    except Exception:
        unit = "%"
    shown = f"${value:,.0f}" if unit == "$" else f"{value:g}{unit}"
    by = f" by {_mdy(deadline)}" if deadline else ""
    return f"your goal of {shown}{by}"


def target_for(restaurant_id, metric, db_path=None):
    """The target a module judges `metric` against when the owner set one
    through a goal — ("labor_pct" | "food_cost_pct" | "prime_cost_pct" |
    "nightly_sales" | ...) -> {"value": float, "source": "goal", "goal_id":
    int, "until": date|None, "label": "your goal of 28% by 12/1/26",
    "metric": str, "set_by": int|None} — or None, and the module keeps its
    own setting. Only an ACTIVE goal (a teammate's proposal is not one until
    the owner confirms it) whose date has not passed: a missed goal is shown
    as missed in Goals, and stops overriding the module's own target."""
    if not restaurant_id:
        return None
    key = _METRIC_ALIASES.get(str(metric or "").strip().lower(), str(metric or "").strip())
    if not key:
        return None
    ck = (int(restaurant_id), key, db_path or "")
    memo = _request_memo()
    if memo is not None and ck in memo:
        return dict(memo[ck]) if memo[ck] else None
    out = None
    try:
        conn = get_conn(db_path)
        try:
            # The newest active goal still in date (memory re-audit R3,
            # QUALITY-10): a missed newest goal no longer hides an older one
            # on the same metric whose date has not passed.
            row = conn.execute("SELECT id, metric, target, deadline, created_by FROM owner_goals "
                               "WHERE restaurant_id=? AND metric=? AND status='active' "
                               "AND (deadline IS NULL OR deadline='' OR deadline >= ?) ORDER BY id DESC LIMIT 1",
                               (int(restaurant_id), key, _local_today(restaurant_id).isoformat())).fetchone()
        finally:
            conn.close()
        if row is not None and row["target"] is not None:
            until = _parse_day(row["deadline"])
            if until is None or until >= _local_today(restaurant_id):
                value = float(row["target"])
                out = {"value": value, "source": "goal", "goal_id": int(row["id"]), "until": until,
                       "label": _goal_label(key, value, until), "metric": key, "set_by": row["created_by"]}
    except Exception as e:
        log.debug("owner_memory: target_for %s/%s unreadable: %s", restaurant_id, key, e)
        out = None
    if memo is not None:
        memo[ck] = dict(out) if out else None
    return out


# ── the sales audit, as the restaurant's founding memory ────────────────────
#
# The in-person audit asks the owner what frustrates them, where money leaks,
# what their targets are and how purchasing works — and none of it became
# memory, so Ask asked again (memory audit 9/29/26, sales_audit). When an
# audit is linked to the account (promise.link), the owner's own answers to
# the questions below are seeded as facts sourced "Sales audit M/D/YY" —
# visible in Account and forgettable like any other — with the report-safe
# conversation insights the report itself showed; and the targets they gave
# become PROPOSED goals the owner confirms. Once per audit and account.
# Never seeded: contact details, internal notes, sales observations, money
# guesses, or anything about how to sell to them.
AUDIT_FACT_QUESTIONS = (
    ("lab_frustration", "The labor problem that frustrates them most", ("labor",)),
    ("lab_waste_where", "Where they feel labor is wasted", ("labor",)),
    ("lab_hard_shifts", "The shifts hardest to staff", ("labor", "schedule")),
    ("lab_ot_why", "Why overtime happens", ("labor",)),
    ("lab_ot_positions", "Positions that run overtime", ("labor",)),
    ("wl_peak_times", "Peak wait times", ("labor", "schedule")),
    ("food_waste_where", "Where most waste happens", ("food",)),
    ("food_high_cost_items", "High-cost items", ("food",)),
    ("food_low_margin_items", "Low-margin items", ("food",)),
    ("food_purchasing", "How purchasing works", ("food",)),
    ("food_invoice_review", "Who reviews invoices", ("food",)),
    ("rev_top_complaints", "Top guest complaints at the audit", ("reviews",)),
    ("rev_top_compliments", "Recurring compliments", ("reviews", "marketing")),
    ("mkt_promos", "Promotions and discounts they run", ("marketing",)),
    ("ops_wish_sooner", "What they wish they knew sooner", ()),
    ("ops_leaking", "Where they think money leaks", ()),
    ("ops_fix_tomorrow", "What they would fix tomorrow", ()),
    ("pri_top3", "Their top concerns", ()),
    ("pri_losing_money", "Where they feel they lose money", ()),
    ("pri_metric", "The number they watch most closely", ()),
    ("pri_three_things", "The three things they want to know every morning", ()),
    ("pri_improve_year", "What they are trying to improve this year", ()),
)
# The audit's stated targets -> the goal each proposes.
AUDIT_TARGETS = (("lab_target_pct", "labor_pct"), ("food_target_pct", "food_cost_pct"))
# What the owner saw on the day of the audit (complaints, wait times, waste)
# is a point-in-time observation: it holds until a review date, then leaves
# for the archive with "its review date passed — put it back if it still
# holds" (memory re-audit 9/29/26, R3 audit_memory). Their stated aims
# (pri_*) are kept without one.
AUDIT_REVIEW_DAYS = 180
AUDIT_REVIEW_MIN_DAYS = 30
_AUDIT_CATEGORY_MODULES = {"labor": ("labor",), "food": ("food",), "bar": ("food",), "reviews": ("reviews",),
                           "marketing": ("marketing",), "waitlist": ("labor",), "operations": (),
                           "technology": ()}


def seed_from_audit(audit_id, restaurant_id, db_path=None) -> dict:
    """Seed the account's memory from a linked audit (see above). Returns
    {"facts": n, "goals": [metric...], "skipped": reason|None}. Never
    raises into the link that called it."""
    out = {"facts": 0, "goals": [], "skipped": None}
    if not audit_id or not restaurant_id:
        out["skipped"] = "no audit or account"
        return out
    marker = f"{int(audit_id)}:{int(restaurant_id)}"
    # Checked here, CLAIMED only once the audit was read and seeded (memory
    # re-audit R3, QUALITY-20): claiming first meant an unreadable audit, or
    # a crash midway, skipped the founding memory forever. A second seed is
    # harmless anyway — every write is idempotent on its text and author.
    try:
        import ops
        if ops.marker_created_at("audit_memory", marker):
            out["skipped"] = "already seeded from this audit"
            return out
    except Exception as e:
        log.debug("owner_memory: audit seed marker unavailable: %s", e)
    try:
        import sales_audits
        audit = sales_audits.get_audit(audit_id, db_path=db_path or _default_db())
    except Exception as e:
        out["skipped"] = f"audit unreadable: {e}"
        return out
    if not audit:
        out["skipped"] = "no such audit"
        return out
    when = _mdy(audit.get("audit_date"))
    label = f"Sales audit {when}" if when else "Sales audit"
    answers = audit.get("answers") or {}
    today = _local_today(restaurant_id)
    audited = _parse_day(audit.get("audit_date")) or today
    review_on = max(audited + timedelta(days=AUDIT_REVIEW_DAYS), today + timedelta(days=AUDIT_REVIEW_MIN_DAYS))
    for qid, what, mods in AUDIT_FACT_QUESTIONS:
        val = answers.get(qid)
        if not isinstance(val, str) or not val.strip():
            continue
        text = f"{what}: {' '.join(val.split())}"
        kind = "goal" if qid == "pri_improve_year" and not looks_like_target(val) else "context"
        try:
            remember(restaurant_id, text, kind=kind, modules=list(mods), audience="principals", user=None,
                     source=label, origin="audit", author_label=label, db_path=db_path, today=today,
                     valid_until=None if qid.startswith("pri_") else review_on.isoformat())
            out["facts"] += 1
        except ValueError:
            continue
    # The report-safe insights the report itself carried (sales_audits.
    # public_view: drawn from audit notes Will ticked for the report).
    try:
        view = sales_audits.public_view(audit) or {}
        for ins in view.get("conversation") or []:
            text = " ".join(str(ins.get("text") or "").split())
            if not text:
                continue
            mods = _AUDIT_CATEGORY_MODULES.get(str(ins.get("category") or ""), ())
            try:
                remember(restaurant_id, text, kind="context", modules=list(mods), audience="principals", user=None,
                         source=label, origin="audit", author_label=label, db_path=db_path, today=today,
                         valid_until=review_on.isoformat())
                out["facts"] += 1
            except ValueError:
                continue
    except Exception as e:
        log.debug("owner_memory: audit insights not seeded: %s", e)
    # The targets they stated become goals the owner confirms.
    try:
        import goals
        waiting = {g["metric"] for g in goals.proposed(restaurant_id, db_path=db_path or _default_db())}
        active = {g["metric"] for g in goals.progress(restaurant_id, db_path=db_path or _default_db())}
        for qid, metric in AUDIT_TARGETS:
            try:
                target = float(str(answers.get(qid)).replace("%", "").strip())
            except (TypeError, ValueError):
                continue
            if not (0 < target < 100) or metric in waiting or metric in active:
                continue
            goals.propose_goal(restaurant_id, metric, target, note=f"The target you gave at the {label.lower()}",
                               user_id=None, db_path=db_path or _default_db(), authority="audit", source="audit")
            out["goals"].append(metric)
    except Exception as e:
        log.debug("owner_memory: audit goals not proposed: %s", e)
    try:
        import ops
        ops.claim_marker("audit_memory", marker)
    except Exception as e:
        log.debug("owner_memory: audit seed marker not claimed: %s", e)
    invalidate(restaurant_id)
    return out


# ── Account's view of the memory ────────────────────────────────────────────

_REASON_LABELS = {"evicted": "its lane was full", "expired": "its date passed",
                  "retracted": "you used that recommendation again",
                  "forgotten": "you asked Cavnar AI to forget it",
                  "replaced": "a newer note replaced it",
                  "contradicted": "your newer ratings no longer asked for it",
                  "author_left": "the person who added it no longer has access"}


def _principal(user) -> bool:
    try:
        from permissions import answer_authority
        return user is None or answer_authority(user) in ("principal", "admin")
    except Exception:
        return False


def _archive_row_for(restaurant_id, archive_id, user, db_path=None):
    """One archived fact `user` may read (the account view's own rule), or
    None — read against the table by id, not the rendered list."""
    import models
    rows = models.get_ask_memory_archive(restaurant_id, limit=1, db_path=db_path or models.DB_PATH,
                                         archive_id=archive_id, viewer_id=(user or {}).get("id"),
                                         principal=None if user is None else _principal(user))
    return rows[0] if rows else None


def _may_restore(row, user) -> bool:
    if _principal(user):
        return True
    return row.get("user_id") is not None and user is not None and row.get("user_id") == user.get("id")


def restore(restaurant_id, archive_id, user=None, valid_until=None, due_on=None, today=None, db_path=None) -> dict:
    """Put an archived fact back (Account's "Put it back"). {"fact",
    "valid_until", "due_on"} | {"error", "status"}.

    By id against the archive with the viewer's own filter (memory re-audit
    9/29/26, R3 archive_restore — it only accepted an id in the 30 newest
    rendered rows). A date that has passed is never put back as it was — the
    next read archived it again: a new `valid_until`/`due_on` is taken
    (and must be in the future), otherwise a passed validity is cleared and
    a follow-up is due in a week. A departed login's note comes back as the
    restoring account holder's own, owner-only."""
    import models
    row = _archive_row_for(restaurant_id, archive_id, user, db_path=db_path)
    if row is None:
        return {"error": "No fact like that.", "status": 404}
    if not _may_restore(row, user):
        return {"error": "Only the owner or the person who added it can put it back.", "status": 403}
    today = today or _local_today(restaurant_id)
    over = {}
    new_until, new_due = _parse_day(valid_until), _parse_day(due_on)
    if (valid_until and new_until is None) or (due_on and new_due is None):
        return {"error": "Give the date as YYYY-MM-DD.", "status": 400}
    if (new_until and new_until < today) or (new_due and new_due < today):
        return {"error": "That date has already passed.", "status": 400}
    old_until = _parse_day(row.get("valid_until"))
    if new_until:
        over["valid_until"] = new_until.isoformat()
    elif old_until is not None and old_until < today:
        over["valid_until"] = None
    old_due = _parse_day(row.get("due_on"))
    if new_due:
        over["due_on"] = new_due.isoformat()
    elif (row.get("kind") == "followup" and old_due is not None
          and old_due + timedelta(days=FOLLOWUP_GRACE_DAYS) < today):
        over["due_on"] = (today + timedelta(days=7)).isoformat()
    if row.get("reason") == "author_left" and user is not None:
        who = author_of(user)
        was = row.get("author_label") or "a teammate"
        over.update({"user_id": who["user_id"], "author_label": who["label"], "authority": who["authority"],
                     "audience": "principals", "source": f"Kept from {was}"[:160]})
    fact = models.restore_ask_fact(restaurant_id, archive_id, db_path=db_path or models.DB_PATH, overrides=over)
    if not fact:
        return {"error": "No fact like that.", "status": 404}
    invalidate(restaurant_id)
    merged = dict(row, **over)
    return {"fact": fact, "valid_until": merged.get("valid_until"), "due_on": merged.get("due_on")}


def dismiss(restaurant_id, archive_id, user=None, db_path=None) -> dict:
    """Let an archived fact go for good (Account's "Dismiss") — an owner's
    evicted rule is otherwise kept until someone does (ops._RETENTION_ONLY)."""
    import models
    row = _archive_row_for(restaurant_id, archive_id, user, db_path=db_path)
    if row is None:
        return {"error": "No fact like that.", "status": 404}
    if not _may_restore(row, user):
        return {"error": "Only the owner or the person who added it can dismiss it.", "status": 403}
    models.delete_ask_archive_rows(restaurant_id, [row["id"]], db_path=db_path or models.DB_PATH)
    return {"dismissed": row["fact"]}


def pin(restaurant_id, fact_id, user=None, pinned=True, db_path=None) -> dict:
    """An account holder pins a fact out of lane eviction (or unpins it) —
    at most ASK_MEMORY_PIN_MAX of one kind. {"pinned": bool} | {"error",
    "status"}."""
    import models
    if not _principal(user):
        return {"error": "Only the owner can pin a note.", "status": 403}
    rows = facts_for(restaurant_id, viewer=user, db_path=db_path, include_expired=True)
    try:
        row = next((r for r in rows if r["id"] == int(fact_id)), None)
    except (TypeError, ValueError):
        row = None
    if row is None:
        return {"error": "No fact like that.", "status": 404}
    if pinned and not row.get("pinned"):
        kind = row.get("kind") or "context"
        n = sum(1 for r in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH, kinds=[kind])
                if r.get("pinned"))
        if n >= models.ASK_MEMORY_PIN_MAX:
            return {"error": f"Up to {models.ASK_MEMORY_PIN_MAX} pinned notes of one kind — unpin one first.",
                    "status": 400}
    models.set_ask_fact_pinned(restaurant_id, row["id"], bool(pinned), db_path=db_path or models.DB_PATH)
    invalidate(restaurant_id)
    return {"pinned": bool(pinned), "fact": row["fact"]}


ARCHIVE_PAGE = 30


def account_view(restaurant_id, user, db_path=None, archive_before=None, archive_limit=ARCHIVE_PAGE) -> dict:
    """What Account shows (GET /account/memory): the facts this login may
    read, each with who added it, its type, its dates and whether this login
    may forget (or, an account holder, pin) it; the lanes — counted over the
    facts THIS login may read (memory re-audit R3, PEOPLE-17: they counted
    every row, so a manager learned how many facts were kept from them) —
    and, for an account holder, how many private notes other people keep
    (a count, never the text); and what left without anyone asking (the
    archive), filtered for this login BEFORE its page is cut, paged by
    `archive_before` (`archive_more` says there is an older page), so
    forgetting is visible and reversible for as long as the archive keeps it."""
    import models
    principal = _principal(user)
    rows = facts_for(restaurant_id, viewer=user, db_path=db_path)
    org_ok = may_set_org(restaurant_id, user, db_path=db_path)
    departed = departed_author_ids(restaurant_id, db_path=db_path) if principal else set()
    facts = []
    for r in rows:
        facts.append({"id": r["id"], "fact": r["fact"], "kind": r.get("kind") or "context",
                      "source": r.get("source"), "created_at": r.get("created_at"),
                      "created_on": _mdy(str(r.get("created_at") or "")[:10]),
                      "author": _who(r), "author_id": r.get("user_id"), "audience": r.get("audience") or "team",
                      "modules": sorted(_modules_of(r)), "subject": r.get("subject"),
                      "valid_until": r.get("valid_until"), "valid_until_label": _mdy(r.get("valid_until")),
                      "due_on": r.get("due_on"), "due_label": _mdy(r.get("due_on")),
                      "origin": r.get("origin"),
                      # Another location's organisation-wide fact is forgotten
                      # where it was kept (R6, PEOPLE-13).
                      "can_forget": _may_edit(r, user) and r.get("from_location") is None,
                      "scope": "org" if r.get("scope") == "org" else "location",
                      "from_location": r.get("from_location"),
                      "can_set_scope": r.get("from_location") is None and org_ok,
                      # Ask's remember tool writes the model's summary of what
                      # was said — shown as such (re-audit R3, QUALITY-11).
                      "worded_by": "Cavnar AI" if r.get("origin") == "ask" else None,
                      "confirmed_by": r.get("confirmed_by"),
                      "confirmed_on": _mdy(str(r.get("confirmed_at") or "")[:10]) if r.get("confirmed_by") else "",
                      "pinned": bool(r.get("pinned")), "can_pin": principal and r.get("from_location") is None,
                      "author_left": r.get("user_id") in departed})
    counts = {}
    for r in rows:
        k = r.get("kind") or "context"
        counts[k] = counts.get(k, 0) + 1
    lanes = [{"kind": k, "count": counts.get(k, 0), "cap": models.ASK_MEMORY_CAPS[k]} for k in KINDS]
    others_private = 0
    if principal and user is not None:
        seen = {r["id"] for r in rows}
        others_private = sum(1 for r in models.get_ask_memory(restaurant_id, db_path=db_path or models.DB_PATH)
                             if r["id"] not in seen and (r.get("audience") or "team") == "author")
    archived = []
    try:
        limit = max(1, min(int(archive_limit or ARCHIVE_PAGE), 100))
    except (TypeError, ValueError):
        limit = ARCHIVE_PAGE
    page = models.get_ask_memory_archive(restaurant_id, limit=limit + 1, db_path=db_path or models.DB_PATH,
                                         viewer_id=(user or {}).get("id"),
                                         principal=None if user is None else principal, before_id=archive_before)
    more = len(page) > limit
    for a in page[:limit]:
        label = _REASON_LABELS.get(a.get("reason"), a.get("reason"))
        if a.get("reason") == "expired" and a.get("origin") == "audit":
            label = "its review date passed — put it back if it still holds"
        archived.append({"id": a["id"], "fact": a["fact"], "kind": a.get("kind") or "context",
                         "author": a.get("author_label") or a.get("source"), "reason": a.get("reason"),
                         "reason_label": label,
                         "archived_on": _mdy(str(a.get("archived_at") or "")[:10]),
                         "can_restore": _may_restore(a, user)})
    return {"facts": facts, "lanes": lanes, "others_private": others_private, "archived": archived,
            "archive_more": more, "can_set_org": org_ok}
